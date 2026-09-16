"""
Discovery agent: the goal-driven observe -> decide -> act loop (assignment
Section 3.1) that drives a real live surface and, on success, emits a
structured Capability artifact (Section 3.2).

The model never gets raw control of the browser. Every tool call it makes is
executed through the same guardrail + locator layer that replay uses, and
every accepted action becomes one recorded Step. This is deliberate: what the
agent actually did during discovery IS the artifact, not a paraphrase of it.
"""
from __future__ import annotations

import time
import uuid
from urllib.parse import urljoin

from .artifact import (
    ActionType,
    AllowlistScope,
    ArtifactMetadata,
    Capability,
    Checkpoint,
    ErrorClass,
    ErrorHandler,
    InputParam,
    Locator,
    LocatorSpec,
    LocatorStrategy,
    OutputField,
    RiskLevel,
    Step,
)
from .guardrails import AllowlistPolicy, GuardrailViolation
from .llm_provider import LLMProvider
from . import locator as loc_mod
from . import perception
from .logging_utils import EvidenceLogger

RISKY_KEYWORDS = ("confirm", "submit", "create", "open", "delete", "approve", "save")


def _sanitize_tool_input(tool_name: str, tool_input: dict) -> dict:
    """Redact credential values before they ever hit the evidence log -- the
    literal password/username must not be persisted anywhere, logs included."""
    if tool_name != "type_text":
        return tool_input
    name_lower = str(tool_input.get("name", "")).lower()
    if "password" in name_lower or "passwd" in name_lower or "username" in name_lower or "user name" in name_lower:
        sanitized = dict(tool_input)
        sanitized["text"] = "<redacted-credential>"
        return sanitized
    return tool_input


def _templatize(text: str, value_to_param: dict[str, str]) -> str:
    """Replace any occurrence of a known concrete input-parameter value with
    its {{param}} placeholder, so recorded checkpoints/URLs generalize across
    invocations instead of being pinned to this one discovery run's values.
    Longest values first so e.g. '100' inside '1005' doesn't misfire."""
    out = text
    for value, name in sorted(value_to_param.items(), key=lambda kv: -len(kv[0])):
        if value:
            out = out.replace(value, "{{" + name + "}}")
    return out


def classify_risk(action: ActionType, name: str, role: str = "button") -> RiskLevel:
    """Heuristic risk classification (documented limitation: this is a keyword
    heuristic, not semantic understanding -- see /REPORT.md Section 6). A CLICK
    is only considered for RISKY status if it's a `button`-role control (a form
    submission / state-changing action); plain `link`-role navigation (e.g. "Open
    a new sub-account" as a link to a *form*, not a submission) is never treated
    as risky by keyword alone, since navigating to view a form has no side
    effects. This was a real false positive caught during testing: a link whose
    accessible name happened to contain "open" was blocked as RISKY even though
    clicking it only navigates to a form."""
    if action == ActionType.CLICK and role == "button" and any(k in name.lower() for k in RISKY_KEYWORDS):
        return RiskLevel.RISKY
    return RiskLevel.SAFE


def default_error_handlers_for_bank_app() -> list[ErrorHandler]:
    """Known runtime conditions for this target app. In a real system these
    accumulate over time (per vendor product) rather than being rediscovered
    from scratch on every recording -- see /REPORT.md section 4."""
    return [
        ErrorHandler(
            handler_id="member_not_found",
            match_kind="text_present",
            match_value="No member found",
            classification=ErrorClass.BUSINESS_OUTCOME,
            outcome_code="MEMBER_NOT_FOUND",
            message_template="No member found with the given ID.",
        ),
        ErrorHandler(
            handler_id="permission_denied",
            match_kind="text_present",
            match_value="PERMISSION_DENIED",
            classification=ErrorClass.BUSINESS_OUTCOME,
            outcome_code="PERMISSION_DENIED",
            message_template="The member's account does not permit this action (e.g. frozen).",
        ),
        ErrorHandler(
            handler_id="validation_error",
            match_kind="text_present",
            match_value="VALIDATION_ERROR",
            classification=ErrorClass.BUSINESS_OUTCOME,
            outcome_code="VALIDATION_ERROR",
            message_template="The submitted input failed a business validation rule.",
        ),
        ErrorHandler(
            handler_id="transient_interstitial",
            match_kind="text_present",
            match_value="processing your request",
            classification=ErrorClass.RECOVERABLE,
            outcome_code="TRANSIENT_INTERSTITIAL",
            message_template="Transient processing interstitial shown; dismissed and retried.",
            recovery_action="dismiss_and_retry",
            max_recovery_attempts=2,
        ),
    ]


class DiscoveryError(Exception):
    pass


class EscalationRequested(Exception):
    def __init__(self, reason: str, step_index: int):
        self.reason = reason
        self.step_index = step_index
        super().__init__(reason)


class DiscoveryAgent:
    def __init__(
        self,
        page,
        provider: LLMProvider,
        allowlist: AllowlistScope,
        evidence: EvidenceLogger,
        target_app_id: str,
        base_url: str,
        surface_type: str = "legacy_web",
        max_steps: int = 20,
        timeout_s: int = 180,
    ):
        self.page = page
        self.provider = provider
        self.policy = AllowlistPolicy(scope=allowlist)
        self.evidence = evidence
        self.target_app_id = target_app_id
        self.base_url = base_url
        self.surface_type = surface_type
        self.max_steps = max_steps
        self.timeout_s = timeout_s

    def run(
        self,
        goal: str,
        initial_url: str,
        input_values: dict[str, str],
        input_param_specs: list[InputParam],
        output_field_specs: list[OutputField],
        capability_name: str,
        credentials: dict[str, str] | None = None,
    ) -> Capability:
        run_id = self.evidence.run_id
        self.evidence.event("discovery_started", goal=goal, initial_url=initial_url,
                             provider_is_real=self.provider.is_real())

        self.policy.check_url(initial_url)
        self.page.goto(initial_url)

        # Credentials (if any) are folded into the system prompt ONLY -- an
        # in-memory string used for this LLM call and never written to the
        # evidence log or the artifact's persisted `description` field (that's
        # `goal`, above, which the caller must keep credential-free; see
        # cli.py's goal templates and guardrails.py for the redaction story).
        credentials_line = ""
        if credentials:
            credentials_line = (
                "LOGIN CREDENTIALS (use only to log in; never repeat these back or reference "
                "them by value in your reasoning text): " +
                ", ".join(f"{k}={v}" for k, v in credentials.items()) + "\n"
            )
        system_prompt = (
            "You are an automation agent operating a real internal web application on behalf "
            "of a bank/credit-union operator. You act ONLY through the provided tools; you never "
            "see raw HTML, only an accessibility-tree observation of the current screen. "
            f"{credentials_line}"
            f"GOAL: {goal}\n"
            "Work step by step. After each action you will be shown the new page state. "
            "When the goal is verifiably achieved, call `done` with the exact text/state that "
            "proves it. If you are blocked, unsure it is safe to proceed, or stuck for more than "
            "a couple of attempts, call `escalate` instead of guessing."
        )
        messages: list[dict] = [{"role": "user", "content": system_prompt}]

        steps: list[Step] = []
        value_to_param = {v: k for k, v in input_values.items()}  # concrete value -> param name
        outputs_collected: dict[str, str] = {}
        start = time.time()

        for i in range(self.max_steps):
            if time.time() - start > self.timeout_s:
                raise DiscoveryError(f"Discovery timed out after {self.timeout_s}s")

            obs = perception.render_for_llm(self.page)
            messages.append({"role": "user", "content": f"OBSERVATION (step {i}):\n{obs}"})
            self.evidence.event("observation", step_index=i, url=self.page.url)

            decision = self.provider.decide(messages)
            self.evidence.event(
                "llm_decision", step_index=i, tool=decision.tool_name,
                tool_input=_sanitize_tool_input(decision.tool_name, decision.tool_input),
                reasoning=decision.assistant_text,
            )
            # Record the assistant turn for conversation continuity (Anthropic provider only
            # meaningfully uses this; scripted provider ignores it).
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": decision.assistant_text or "(no text)"},
            ] if not decision.tool_name else [
                {"type": "tool_use", "id": f"call_{i}", "name": decision.tool_name, "input": decision.tool_input},
            ]})

            if decision.tool_name == "escalate":
                reason = decision.tool_input.get("reason", "model requested escalation")
                self.evidence.screenshot(self.page, "escalation")
                self.evidence.event("escalation_requested", reason=reason, step_index=i)
                raise EscalationRequested(reason, i)

            if decision.tool_name == "done":
                checkpoint_text = decision.tool_input.get("checkpoint_text", "")
                checkpoint_text = _templatize(checkpoint_text, value_to_param)
                self.evidence.event("goal_declared_done", checkpoint_text=checkpoint_text, step_index=i)
                self.evidence.screenshot(self.page, "final_success")
                return self._build_capability(
                    capability_name, goal, steps, checkpoint_text, run_id,
                    input_param_specs, output_field_specs, outputs_collected,
                )

            if not decision.tool_name:
                # Model didn't call a tool at all -- can't safely continue.
                self.evidence.screenshot(self.page, "no_tool_call")
                raise EscalationRequested("Model did not call a tool; unsafe to guess.", i)

            try:
                step = self._execute_and_record(decision, value_to_param, i, outputs_collected)
                steps.append(step)
                # Feed the tool result back so a real LLM sees the outcome next turn.
                messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": f"call_{i}", "content": "ok"}],
                })
            except GuardrailViolation as e:
                self.evidence.event("guardrail_blocked", step_index=i, error=str(e))
                raise
            except Exception as e:  # noqa: BLE001
                self.evidence.event("action_failed", step_index=i, error=str(e))
                self.evidence.screenshot(self.page, f"action_failed_{i}")
                messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": f"call_{i}",
                                  "content": f"ERROR: {e}", "is_error": True}],
                })

        self.evidence.event("discovery_max_steps_exceeded")
        raise DiscoveryError("Max steps exceeded without reaching goal")

    # -- action execution ----------------------------------------------

    def _execute_and_record(self, decision, value_to_param, step_index, outputs_collected) -> Step:
        tool = decision.tool_name
        inp = decision.tool_input
        step_id = f"s{step_index}"
        url_before = self.page.url

        if tool == "navigate":
            url = inp["url"]
            full_url = urljoin(self.base_url, url)
            self.policy.check_url(full_url)
            self.policy.check_action(ActionType.NAVIGATE)
            self.page.goto(full_url)
            step = Step(step_id=step_id, action=ActionType.NAVIGATE,
                        description=inp.get("reasoning", ""), value=full_url,
                        risk_level=RiskLevel.SAFE, timeout_ms=8000)

        elif tool == "click":
            self.policy.check_action(ActionType.CLICK)
            spec = loc_mod.spec_for_role(inp["role"], inp["name"])
            element = loc_mod.resolve(self.page, spec)
            risk = classify_risk(ActionType.CLICK, inp["name"], role=inp["role"])
            element.click()
            self.page.wait_for_load_state("domcontentloaded", timeout=5000)
            checkpoint = None
            if self.page.url != url_before:
                url_suffix = _templatize(self.page.url.split("://", 1)[-1], value_to_param)
                checkpoint = Checkpoint(kind="url_contains", value=url_suffix)
            step = Step(step_id=step_id, action=ActionType.CLICK,
                        description=inp.get("reasoning", ""), target=spec,
                        risk_level=risk, checkpoint=checkpoint, timeout_ms=8000)

        elif tool == "type_text":
            self.policy.check_action(ActionType.TYPE)
            spec = loc_mod.spec_for_role(inp["role"], inp["name"])
            # form inputs often expose role "textbox" via label; try label spec too as fallback
            spec.fallbacks.append(Locator(strategy=LocatorStrategy.LABEL, value=inp["name"]))
            element = loc_mod.resolve(self.page, spec)
            text = inp["text"]
            element.fill(text)
            name_lower = inp["name"].lower()
            if "password" in name_lower or "passwd" in name_lower:
                # Never persist credentials into the artifact (Section 3.4). The
                # replayed step references a placeholder that must be resolved
                # from a secret store / env var at replay time.
                value_out = "{{OPERATOR_PASSWORD}}"
            elif "username" in name_lower or "user name" in name_lower or name_lower == "user":
                value_out = "{{OPERATOR_USERNAME}}"
            else:
                templated = value_to_param.get(text)
                value_out = "{{" + templated + "}}" if templated else text
            step = Step(step_id=step_id, action=ActionType.TYPE,
                        description=inp.get("reasoning", ""), target=spec,
                        value=value_out, risk_level=RiskLevel.SAFE, timeout_ms=8000)

        elif tool == "select_option":
            self.policy.check_action(ActionType.SELECT)
            spec = loc_mod.spec_for_role(inp["role"], inp["name"])
            spec.primary = Locator(strategy=LocatorStrategy.CSS, value="select")  # legacy <select> has no accessible role match by name reliably
            element = self.page.locator("select").first
            element.select_option(label=inp["option_label"])
            step = Step(step_id=step_id, action=ActionType.SELECT,
                        description=inp.get("reasoning", ""), target=spec,
                        value=inp["option_label"], risk_level=RiskLevel.SAFE, timeout_ms=8000)

        elif tool == "extract":
            self.policy.check_action(ActionType.EXTRACT)
            spec = loc_mod.spec_for_row_label(inp["row_label"])
            element = loc_mod.resolve(self.page, spec)
            text_value = element.inner_text()
            outputs_collected[inp["output_name"]] = text_value
            step = Step(step_id=step_id, action=ActionType.EXTRACT,
                        description=inp.get("reasoning", ""), target=spec,
                        extract_as=inp["output_name"], risk_level=RiskLevel.SAFE, timeout_ms=8000)

        else:
            raise DiscoveryError(f"Unknown tool call: {tool}")

        self.evidence.event("step_recorded", step=step.model_dump())
        return step

    def _build_capability(
        self, name, goal, steps, checkpoint_text, run_id,
        input_param_specs, output_field_specs, outputs_collected,
    ) -> Capability:
        # Backfill source_step: we know exactly which step extracted each
        # named output (Step.extract_as), so there's no reason to leave the
        # schema's source_step field empty -- do it here rather than asking
        # the caller to guess it before the steps even exist.
        extract_step_by_name = {s.extract_as: s.step_id for s in steps if s.extract_as}
        resolved_output_fields = [
            of.model_copy(update={"source_step": extract_step_by_name.get(of.name, of.source_step)})
            for of in output_field_specs
        ]
        cap = Capability(
            artifact_id=f"cap_{uuid.uuid4().hex[:10]}",
            name=name,
            description=goal,
            target_app_id=self.target_app_id,
            base_url=self.base_url,
            surface_type=self.surface_type,
            input_params=input_param_specs,
            output_fields=resolved_output_fields,
            steps=steps,
            success_checkpoint=Checkpoint(kind="text_contains", value=checkpoint_text),
            error_handlers=default_error_handlers_for_bank_app(),
            allowlist_scope=self.policy.scope,
            approval_state="draft",
            metadata=ArtifactMetadata(
                discovery_run_id=run_id,
                discovery_model=getattr(self.provider, "model", "scripted-test"),
                tags=[self.target_app_id],
            ),
        )
        return cap
