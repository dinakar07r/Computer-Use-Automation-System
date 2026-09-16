import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.cua.artifact import (
    ActionType, AllowlistScope, ArtifactMetadata, Capability, Checkpoint,
    ErrorClass, ErrorHandler, InputParam, OutputField, RiskLevel, Step,
)
from src.cua.guardrails import (
    AllowlistPolicy, GuardrailViolation, check_risky_step, is_sensitive_field_name,
    redact_field, redact_text,
)
from src.cua.discovery_agent import classify_risk, _templatize


def make_minimal_capability(approval_state="draft", steps=None):
    return Capability(
        artifact_id="cap_test",
        name="test_cap",
        description="test",
        target_app_id="app",
        base_url="http://127.0.0.1:5055",
        surface_type="legacy_web",
        steps=steps or [],
        success_checkpoint=Checkpoint(kind="text_contains", value="Done"),
        allowlist_scope=AllowlistScope(
            allowed_domains=["127.0.0.1"],
            allowed_url_prefixes=["http://127.0.0.1:5055"],
        ),
        approval_state=approval_state,
        metadata=ArtifactMetadata(discovery_run_id="r1", discovery_model="test"),
    )


# -- artifact schema ---------------------------------------------------------

def test_capability_round_trips_through_json():
    cap = make_minimal_capability()
    raw = cap.model_dump_json_pretty()
    cap2 = Capability.model_validate_json(raw)
    assert cap2.artifact_id == cap.artifact_id
    assert cap2.allowlist_scope.allowed_domains == ["127.0.0.1"]


def test_capability_requires_success_checkpoint():
    with pytest.raises(Exception):
        Capability(
            artifact_id="x", name="x", description="x", target_app_id="x",
            base_url="http://x", steps=[],
            allowlist_scope=AllowlistScope(),
            metadata=ArtifactMetadata(discovery_run_id="r", discovery_model="m"),
        )


# -- guardrails: allowlist ----------------------------------------------------

def test_allowlist_blocks_out_of_scope_domain():
    policy = AllowlistPolicy(scope=AllowlistScope(
        allowed_domains=["127.0.0.1"], allowed_url_prefixes=["http://127.0.0.1:5055"]))
    policy.check_url("http://127.0.0.1:5055/members/1")  # ok
    with pytest.raises(GuardrailViolation):
        policy.check_url("http://evil.example.com/steal")


def test_allowlist_blocks_disallowed_action_type():
    policy = AllowlistPolicy(scope=AllowlistScope(
        allowed_domains=["127.0.0.1"], allowed_action_types=[ActionType.NAVIGATE]))
    policy.check_action(ActionType.NAVIGATE)
    with pytest.raises(GuardrailViolation):
        policy.check_action(ActionType.CLICK)


# -- guardrails: risky-step policy -------------------------------------------

def test_risky_step_blocked_on_draft_artifact_without_override():
    with pytest.raises(GuardrailViolation):
        check_risky_step(RiskLevel.RISKY, approval_state="draft", allow_risky=False)


def test_risky_step_allowed_when_approved():
    check_risky_step(RiskLevel.RISKY, approval_state="approved", allow_risky=False)  # no raise


def test_risky_step_allowed_with_explicit_override():
    check_risky_step(RiskLevel.RISKY, approval_state="draft", allow_risky=True)  # no raise


def test_safe_step_always_allowed():
    check_risky_step(RiskLevel.SAFE, approval_state="draft", allow_risky=False)  # no raise


# -- guardrails: redaction ----------------------------------------------------

def test_password_field_name_is_sensitive():
    assert is_sensitive_field_name("password")
    assert is_sensitive_field_name("api_key")
    assert not is_sensitive_field_name("account_type")


def test_redact_field_hides_password_value():
    out = redact_field("password", "hunter2")
    assert "hunter2" not in out
    assert out.startswith("<redacted:")


def test_redact_field_leaves_normal_field_alone():
    out = redact_field("account_type", "Youth Savings")
    assert out == "Youth Savings"


def test_redact_text_hides_ssn_pattern():
    out = redact_text("SSN on file: 123-45-6789")
    assert "123-45-6789" not in out


def test_deep_redact_recurses_into_nested_dict():
    # Regression test: EvidenceLogger.event() originally only redacted
    # top-level string fields, missing sensitive keys nested one level down
    # (e.g. an `input_values` dict containing OPERATOR_PASSWORD). Caught by
    # grepping generated evidence for a literal password during compliance
    # testing -- see /REPORT.md Section 6.
    from src.cua.logging_utils import _deep_redact
    nested = {"input_values": {"member_id": "12345", "OPERATOR_PASSWORD": "hunter2"}}
    out = _deep_redact(nested)
    assert "hunter2" not in str(out)
    assert out["input_values"]["member_id"] == "12345"  # non-sensitive values untouched


# -- discovery: risk classification & templating ------------------------------

def test_classify_risk_flags_confirm_as_risky():
    assert classify_risk(ActionType.CLICK, "Confirm and Open Account") == RiskLevel.RISKY


def test_classify_risk_flags_search_as_safe():
    assert classify_risk(ActionType.CLICK, "Search") == RiskLevel.SAFE


def test_classify_risk_link_navigation_not_flagged_despite_keyword():
    # Regression test: a link named "Open a new sub-account" merely navigates
    # to a form (no side effects) and must NOT be treated as risky just
    # because "open" is a risk keyword -- only button-role (state-changing)
    # clicks are eligible for RISKY classification.
    assert classify_risk(ActionType.CLICK, "Open a new sub-account", role="link") == RiskLevel.SAFE
    assert classify_risk(ActionType.CLICK, "Open a new sub-account", role="button") == RiskLevel.RISKY


def test_templatize_replaces_concrete_value_with_placeholder():
    out = _templatize("http://127.0.0.1:5055/members/55555", {"55555": "member_id"})
    assert out == "http://127.0.0.1:5055/members/{{member_id}}"


def test_templatize_prefers_longest_match_first():
    # "100" should not clobber inside "1005" if both were params (edge case check)
    out = _templatize("value=1005", {"1005": "deposit", "100": "other"})
    assert out == "value={{deposit}}"


# -- replay engine: result contract shape -------------------------------------

def test_replay_result_statuses_are_distinct():
    from src.cua.artifact import ReplayResult, ReplayStatus
    r1 = ReplayResult(status=ReplayStatus.SUCCESS, outputs={"a": 1})
    r2 = ReplayResult(status=ReplayStatus.BUSINESS_OUTCOME, outcome_code="MEMBER_NOT_FOUND")
    r3 = ReplayResult(status=ReplayStatus.FAILURE, failed_step_id="s1", expected="x", observed="y")
    assert r1.status != r2.status != r3.status
    assert r2.outcome_code == "MEMBER_NOT_FOUND"


def test_output_field_source_step_is_backfilled_from_extract_steps():
    # Regression test: OutputField.source_step was defined in the schema but
    # never populated -- caught during a full field-by-field compliance
    # check of a generated artifact. _build_capability now backfills it from
    # the matching Step.extract_as.
    from src.cua.discovery_agent import DiscoveryAgent
    from src.cua.artifact import Step, ActionType, RiskLevel

    class _Stub(DiscoveryAgent):
        def __init__(self):
            self.target_app_id = "app"
            self.base_url = "http://x"
            self.surface_type = "legacy_web"
            self.provider = None
            from src.cua.guardrails import AllowlistPolicy
            from src.cua.artifact import AllowlistScope
            self.policy = AllowlistPolicy(scope=AllowlistScope())

    steps = [
        Step(step_id="s0", action=ActionType.EXTRACT, description="x",
             extract_as="account_number", risk_level=RiskLevel.SAFE),
    ]
    output_specs = [OutputField(name="account_number", type="string", description="d")]
    cap = _Stub()._build_capability(
        "test_cap", "goal", steps, "Done", "run1", [], output_specs, {},
    )
    assert cap.output_fields[0].source_step == "s0"
