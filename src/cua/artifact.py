"""
Artifact schema: the typed, versioned, reusable "capability" a discovery run
produces and a replay run consumes.

Design intent (see /REPORT.md section 2 for the full rationale):
  - Decoupled from the raw LLM transcript. The artifact only contains what's
    needed to replay deterministically: ordered steps, how each target
    element is located (with a fallback chain and a robustness note), typed
    inputs/outputs, and checkpoints.
  - Locators are structured, not "just a selector string" -- each one carries
    a primary strategy plus ranked fallbacks, so replay can degrade
    gracefully under minor UI drift instead of hard-failing on the first
    locator miss.
  - Error handling is part of the contract, not bolted onto the executor.
    `error_handlers` classify runtime conditions into business outcomes,
    recoverable conditions, or hard failures -- see guardrails/replay for how
    this is enforced.
  - Every step carries a risk_level so the executor and the guardrail layer
    can treat irreversible actions conservatively without re-deriving that
    from the action type.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class LocatorStrategy(str, Enum):
    ROLE = "role"          # Playwright get_by_role(role, name=...) - accessibility tree
    LABEL = "label"        # get_by_label - form fields with a <label>
    TEXT = "text"          # get_by_text - visible text content
    TEST_ID = "test_id"    # data-testid - best case, rarely available on legacy apps
    CSS = "css"            # raw CSS selector - last resort, most brittle
    ROW_LABEL = "row_label"  # value in a legacy table row identified by its adjacent label cell


class Locator(BaseModel):
    strategy: LocatorStrategy
    value: str
    role: Optional[str] = None          # e.g. "button", "textbox", "link"
    exact: bool = False


class LocatorSpec(BaseModel):
    """How a target element/control is identified, with graceful degradation."""
    primary: Locator
    fallbacks: list[Locator] = Field(default_factory=list)
    robustness_note: str = Field(
        default="",
        description="Why this strategy should survive minor UI drift (or why it might not).",
    )


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    WAIT_FOR = "wait_for"
    EXTRACT = "extract"
    ASSERT = "assert"
    DISMISS_INTERSTITIAL = "dismiss_interstitial"


class RiskLevel(str, Enum):
    SAFE = "safe"          # read-only or trivially reversible (search, navigate, view)
    RISKY = "risky"        # irreversible / state-changing (create, submit, confirm)


class Checkpoint(BaseModel):
    """A post-condition asserted after a step to confirm it actually landed,
    rather than assuming the action worked."""
    kind: Literal["url_contains", "element_visible", "text_contains", "element_absent"]
    value: str
    role: Optional[str] = None  # used with element_visible/element_absent


class Step(BaseModel):
    step_id: str
    action: ActionType
    description: str
    target: Optional[LocatorSpec] = None
    # Value may reference an input parameter via {{param_name}} templating.
    value: Optional[str] = None
    risk_level: RiskLevel = RiskLevel.SAFE
    checkpoint: Optional[Checkpoint] = None
    # For EXTRACT steps: name under which the extracted value is stored/output.
    extract_as: Optional[str] = None
    timeout_ms: int = 8000


class InputParam(BaseModel):
    name: str
    type: Literal["string", "number", "boolean"]
    required: bool = True
    description: str = ""


class OutputField(BaseModel):
    name: str
    type: Literal["string", "number", "boolean", "object"]
    description: str = ""
    source_step: Optional[str] = None  # which step_id extracts this


class ErrorClass(str, Enum):
    BUSINESS_OUTCOME = "business_outcome"   # e.g. "no such member" - legit result, not a crash
    RECOVERABLE = "recoverable"             # e.g. dismiss interstitial, retry transient load
    HARD_FAILURE = "hard_failure"           # stop and surface a debuggable error


class ErrorHandler(BaseModel):
    """Declarative rule the replay engine checks after each step (and on
    unexpected state) to classify what happened."""
    handler_id: str
    match_kind: Literal["text_present", "element_visible", "url_contains"]
    match_value: str
    classification: ErrorClass
    outcome_code: str = Field(description="Stable machine-readable code, e.g. MEMBER_NOT_FOUND")
    message_template: str = ""
    # Only used when classification == RECOVERABLE
    recovery_action: Optional[Literal["dismiss_and_retry", "wait_and_retry"]] = None
    max_recovery_attempts: int = 1


class AllowlistScope(BaseModel):
    allowed_domains: list[str] = Field(default_factory=list)
    allowed_url_prefixes: list[str] = Field(default_factory=list)
    allowed_action_types: list[ActionType] = Field(default_factory=lambda: list(ActionType))


class ArtifactMetadata(BaseModel):
    discovery_run_id: str
    discovery_model: str
    created_at: float = Field(default_factory=lambda: time.time())
    tags: list[str] = Field(default_factory=list)


class Capability(BaseModel):
    """
    The full artifact: a typed, versioned, agent-invocable capability.

    `schema_version` is the artifact-format version (this document's shape).
    `version` is this specific capability's revision (bump on re-recording).
    """
    schema_version: str = "1.0"
    artifact_id: str
    name: str
    version: int = 1
    description: str

    target_app_id: str
    base_url: str
    surface_type: Literal["web", "legacy_web", "desktop"] = "legacy_web"

    input_params: list[InputParam] = Field(default_factory=list)
    output_fields: list[OutputField] = Field(default_factory=list)

    steps: list[Step]
    success_checkpoint: Checkpoint
    error_handlers: list[ErrorHandler] = Field(default_factory=list)

    allowlist_scope: AllowlistScope
    approval_state: Literal["draft", "approved"] = "draft"

    metadata: ArtifactMetadata

    def model_dump_json_pretty(self) -> str:
        return self.model_dump_json(indent=2)


class ReplayStatus(str, Enum):
    SUCCESS = "SUCCESS"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
    FAILURE = "FAILURE"
    ESCALATED = "ESCALATED"


class ReplayResult(BaseModel):
    """The structured result contract returned to the calling AI agent."""
    status: ReplayStatus
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome_code: Optional[str] = None
    message: Optional[str] = None
    failed_step_id: Optional[str] = None
    expected: Optional[str] = None
    observed: Optional[str] = None
    evidence_dir: Optional[str] = None
    run_id: Optional[str] = None
