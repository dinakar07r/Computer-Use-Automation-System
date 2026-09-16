"""
Deterministic replay engine (assignment Section 3.3) -- the path an AI agent
actually triggers in production. Given a saved Capability artifact and a set
of typed input parameters, it replays the recorded steps with no model in
the decision loop, verifies checkpoints, classifies runtime conditions using
the artifact's declared error_handlers, and returns a structured result:
SUCCESS / BUSINESS_OUTCOME / FAILURE / ESCALATED.
"""
from __future__ import annotations

import re
import time

from .artifact import (
    ActionType,
    Capability,
    Checkpoint,
    ErrorClass,
    ReplayResult,
    ReplayStatus,
    RiskLevel,
    Step,
)
from .escalation import ControlChannel, InterventionRequest
from .guardrails import AllowlistPolicy, GuardrailViolation, check_risky_step
from . import locator as loc_mod
from .locator import LocatorResolutionError
from .logging_utils import EvidenceLogger


class ReplayError(Exception):
    pass


def _fill_template(value: str | None, input_values: dict) -> str | None:
    if value is None:
        return None
    out = value
    for k, v in input_values.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def _check_checkpoint(page, checkpoint: Checkpoint, input_values: dict) -> tuple[bool, str]:
    """Returns (passed, observed_description)."""
    expected_value = _fill_template(checkpoint.value, input_values) or checkpoint.value
    if checkpoint.kind == "url_contains":
        observed = page.url
        return expected_value in observed, observed
    if checkpoint.kind == "text_contains":
        body = page.inner_text("body")
        return expected_value in body, body[:300]
    if checkpoint.kind == "element_visible":
        try:
            loc = page.get_by_role(checkpoint.role or "generic", name=expected_value)
            visible = loc.first.is_visible()
            return visible, f"element visible={visible}"
        except Exception:  # noqa: BLE001
            return False, "element not found"
    if checkpoint.kind == "element_absent":
        try:
            loc = page.get_by_role(checkpoint.role or "generic", name=expected_value)
            return loc.count() == 0, f"count={loc.count()}"
        except Exception:  # noqa: BLE001
            return True, "not found (absent, as expected)"
    return False, "unknown checkpoint kind"


def _scan_error_handlers(page, handlers) -> tuple[object | None, str]:
    """Returns (matched_handler_or_None, page_text_snapshot)."""
    try:
        body_text = page.inner_text("body")
    except Exception:  # noqa: BLE001
        body_text = ""
    for h in handlers:
        if h.match_kind == "text_present" and h.match_value.lower() in body_text.lower():
            return h, body_text
        if h.match_kind == "url_contains" and h.match_value in page.url:
            return h, body_text
    return None, body_text


class ReplayEngine:
    def __init__(self, page, evidence: EvidenceLogger, control: ControlChannel | None = None,
                 cdp_endpoint: str = ""):
        self.page = page
        self.evidence = evidence
        self.control = control
        self.cdp_endpoint = cdp_endpoint

    def replay(
        self,
        capability: Capability,
        input_values: dict,
        allow_risky: bool = False,
        inject_session_timeout_before_step: str | None = None,
        inject_interstitial_before_step: str | None = None,
    ) -> ReplayResult:
        run_id = self.evidence.run_id
        self.evidence.event("replay_started", artifact_id=capability.artifact_id,
                             version=capability.version, input_values=input_values)
        policy = AllowlistPolicy(scope=capability.allowlist_scope)
        outputs: dict = {}

        try:
            policy.check_url(capability.base_url)
            self.page.goto(capability.base_url)
        except GuardrailViolation as e:
            return self._fail(run_id, "init", "allowlisted base_url", str(e), str(e))

        for step in capability.steps:
            if inject_session_timeout_before_step == step.step_id:
                # Deterministic, documented fault injection for evidence purposes
                # (assignment 6.3: "an injected/simulated failure").
                self.page.context.clear_cookies()
                self.evidence.event("fault_injected", kind="session_timeout", before_step=step.step_id)
            if inject_interstitial_before_step == step.step_id:
                # Deterministic RECOVERABLE-path fault injection, mirroring the
                # session-timeout injection above -- hits the test-only
                # /__test__/force_interstitial endpoint via an API-only request
                # (page.request, not page.goto) so it shares the browser
                # context's session cookie without navigating the current page
                # away and losing in-progress form state. The next request to
                # /members/search then shows the transient interstitial
                # regardless of the app's organic every-Nth-request trigger.
                from urllib.parse import urljoin
                self.page.request.get(urljoin(capability.base_url, "/__test__/force_interstitial"))
                self.evidence.event("fault_injected", kind="interstitial", before_step=step.step_id)

            result = self._run_step(step, capability, policy, allow_risky, input_values, outputs, run_id)
            if result is not None:
                return result  # non-SUCCESS terminal result (business outcome / failure / escalated)

        # All steps executed -- verify the overall success checkpoint.
        passed, observed = _check_checkpoint(self.page, capability.success_checkpoint, input_values)
        self.evidence.event("success_checkpoint_check", passed=passed, observed=observed[:300])
        if not passed:
            return self._fail(run_id, "success_checkpoint", capability.success_checkpoint.value, observed,
                               "Final success checkpoint was not observed after all steps completed.")

        self.evidence.event("replay_succeeded", outputs=outputs)
        return ReplayResult(status=ReplayStatus.SUCCESS, outputs=outputs, evidence_dir=str(self.evidence.dir),
                             run_id=run_id)

    # -- internals ------------------------------------------------------

    def _run_step(self, step: Step, capability: Capability, policy: AllowlistPolicy,
                   allow_risky: bool, input_values: dict, outputs: dict, run_id: str,
                   _recovery_depth: int = 0):
        self.evidence.event("step_start", step_id=step.step_id, action=step.action)

        try:
            check_risky_step(step.risk_level, capability.approval_state, allow_risky)
            policy.check_action(step.action)
        except GuardrailViolation as e:
            self.evidence.event("guardrail_blocked", step_id=step.step_id, error=str(e))
            self.evidence.screenshot(self.page, f"guardrail_blocked_{step.step_id}")
            return self._fail(run_id, step.step_id, "guardrail-permitted step", "blocked", str(e))

        try:
            self._execute(step, input_values, outputs)
        except LocatorResolutionError as e:
            return self._handle_bad_state(
                capability, step, run_id, outputs, input_values,
                expected=f"resolvable target for step {step.step_id}",
                observed=str(e), _recovery_depth=_recovery_depth,
                message=f"Could not locate target element for step {step.step_id}.",
            )
        except Exception as e:  # noqa: BLE001
            self.evidence.event("step_exception", step_id=step.step_id, error=str(e))
            return self._handle_bad_state(
                capability, step, run_id, outputs, input_values,
                expected="action to execute without error",
                observed=str(e), _recovery_depth=_recovery_depth,
                message=f"Unexpected error executing step {step.step_id}: {e}",
            )

        if step.checkpoint:
            passed, observed = _check_checkpoint(self.page, step.checkpoint, input_values)
            self.evidence.event("checkpoint_check", step_id=step.step_id, passed=passed, observed=observed[:300])
            if not passed:
                return self._handle_bad_state(
                    capability, step, run_id, outputs, input_values,
                    expected=f"{step.checkpoint.kind}={step.checkpoint.value}",
                    observed=observed, _recovery_depth=_recovery_depth,
                    message=f"Checkpoint failed after step {step.step_id}.",
                )

        # Opportunistic scan: did this step land us on a known business-outcome
        # or recoverable page even though its own checkpoint (if any) passed?
        # The step's own action already succeeded here, so a RECOVERABLE match
        # (e.g. a transient interstitial) must be dismissed and then we move on
        # to the NEXT step -- retrying this step would repeat an action that
        # already happened (see /REPORT.md section 3 for why this distinction
        # matters).
        handler, body_text = _scan_error_handlers(self.page, capability.error_handlers)
        if handler:
            return self._apply_handler(handler, capability, step, run_id, outputs, input_values,
                                        _recovery_depth, retry_step=False)

        self.evidence.event("step_ok", step_id=step.step_id)
        return None  # continue to next step

    def _handle_bad_state(self, capability, step, run_id, outputs, input_values,
                           expected, observed, _recovery_depth, message):
        handler, body_text = _scan_error_handlers(self.page, capability.error_handlers)
        if handler:
            return self._apply_handler(handler, capability, step, run_id, outputs, input_values,
                                        _recovery_depth, retry_step=True)
        # No declared handler matches -- this is a genuine hard failure. Try
        # escalating to a human if a control channel is wired up; otherwise
        # fail fast with a debuggable error.
        self.evidence.screenshot(self.page, f"hard_failure_{step.step_id}")
        if self.control is not None:
            return self._escalate_and_resume(capability, step, run_id, outputs, input_values,
                                               reason=message, _recovery_depth=_recovery_depth)
        return self._fail(run_id, step.step_id, expected, observed, message)

    def _apply_handler(self, handler, capability, step, run_id, outputs, input_values, _recovery_depth,
                        retry_step: bool):
        self.evidence.event("error_handler_matched", handler_id=handler.handler_id,
                             classification=handler.classification, outcome_code=handler.outcome_code)
        if handler.classification == ErrorClass.BUSINESS_OUTCOME:
            self.evidence.screenshot(self.page, f"business_outcome_{handler.outcome_code}")
            return ReplayResult(
                status=ReplayStatus.BUSINESS_OUTCOME, outputs=outputs,
                outcome_code=handler.outcome_code, message=handler.message_template,
                failed_step_id=step.step_id, evidence_dir=str(self.evidence.dir), run_id=run_id,
            )
        if handler.classification == ErrorClass.RECOVERABLE:
            if _recovery_depth >= handler.max_recovery_attempts:
                self.evidence.event("recovery_exhausted", handler_id=handler.handler_id)
                return self._fail(run_id, step.step_id, "recoverable condition to clear",
                                   "still present after max attempts",
                                   f"Recovery for {handler.outcome_code} exhausted.")
            self._recover(handler)
            if not retry_step:
                # The step itself already succeeded; the interstitial was just
                # in the way of observing/continuing. Move on.
                self.evidence.event("recovered_continuing_next_step", step_id=step.step_id,
                                     handler_id=handler.handler_id)
                return None
            # retry the same step after recovering
            return self._run_step(step, capability, AllowlistPolicy(scope=capability.allowlist_scope),
                                   True, input_values, outputs, run_id, _recovery_depth=_recovery_depth + 1)
        # HARD_FAILURE declared explicitly by the artifact
        self.evidence.screenshot(self.page, f"declared_hard_failure_{handler.outcome_code}")
        if self.control is not None:
            return self._escalate_and_resume(capability, step, run_id, outputs, input_values,
                                               reason=handler.message_template, _recovery_depth=_recovery_depth)
        return self._fail(run_id, step.step_id, "no hard failure condition", handler.outcome_code,
                           handler.message_template)

    def _recover(self, handler):
        if handler.recovery_action == "dismiss_and_retry":
            try:
                self.page.get_by_role("link", name="Continue").first.click()
                self.page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:  # noqa: BLE001
                pass
        elif handler.recovery_action == "wait_and_retry":
            time.sleep(1.0)

    def _escalate_and_resume(self, capability, step, run_id, outputs, input_values, reason, _recovery_depth):
        assert self.control is not None
        self.evidence.event("escalating_to_human", step_id=step.step_id, reason=reason)
        req = InterventionRequest(
            run_id=run_id,
            capability_name=capability.name,
            goal=capability.description,
            step_context=f"step {step.step_id}: {step.description}",
            reason=reason,
            screenshot_path=self.evidence.screenshot(self.page, f"escalation_{step.step_id}"),
            cdp_endpoint=self.cdp_endpoint,
        )
        self.control.request_human(req)
        self.evidence.event("waiting_for_human", step_id=step.step_id)
        human_actions = self.control.wait_for_human()
        self.evidence.event("human_handed_back", step_id=step.step_id, human_actions=human_actions)
        # Resume on the SAME page/session: re-attempt this step now that a
        # human has fixed whatever blocked it (e.g. re-authenticated).
        return self._run_step(step, capability, AllowlistPolicy(scope=capability.allowlist_scope),
                               True, input_values, outputs, run_id, _recovery_depth=_recovery_depth)

    def _execute(self, step: Step, input_values: dict, outputs: dict):
        if step.action == ActionType.NAVIGATE:
            self.page.goto(_fill_template(step.value, input_values))
        elif step.action == ActionType.CLICK:
            element = loc_mod.resolve(self.page, step.target, timeout_ms=step.timeout_ms)
            element.click()
            self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        elif step.action == ActionType.TYPE:
            element = loc_mod.resolve(self.page, step.target, timeout_ms=step.timeout_ms)
            element.fill(_fill_template(step.value, input_values) or "")
        elif step.action == ActionType.SELECT:
            self.page.locator("select").first.select_option(label=step.value)
        elif step.action == ActionType.EXTRACT:
            element = loc_mod.resolve(self.page, step.target, timeout_ms=step.timeout_ms)
            outputs[step.extract_as] = element.inner_text()
        elif step.action == ActionType.WAIT_FOR:
            time.sleep(0.5)
        else:
            raise ReplayError(f"Unsupported action in replay: {step.action}")

    def _fail(self, run_id, step_id, expected, observed, message) -> ReplayResult:
        self.evidence.event("replay_failed", step_id=step_id, expected=expected, observed=observed[:500]
                             if isinstance(observed, str) else observed, message=message)
        return ReplayResult(
            status=ReplayStatus.FAILURE, failed_step_id=step_id, expected=expected,
            observed=observed[:500] if isinstance(observed, str) else str(observed),
            message=message, evidence_dir=str(self.evidence.dir), run_id=run_id,
        )
