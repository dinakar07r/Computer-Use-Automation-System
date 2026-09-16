"""
Safety & policy guardrails (assignment Section 3.4).

Three responsibilities, kept deliberately separate and simple:
  1. Allowlist enforcement -- the agent/replay engine may only navigate to,
     or act within, domains/URL prefixes in the allowlist.
  2. Risky-action policy -- RISKY (irreversible) steps are handled
     conservatively: during discovery the agent must not click through a
     RISKY step without it being explicitly reachable and observable in the
     confirm screen; during replay, RISKY steps require the capability to be
     in `approval_state == "approved"` (draft artifacts can run their SAFE
     steps and read up to the confirmation screen, but not execute a RISKY
     step, unless the caller passes allow_risky=True explicitly -- an
     explicit, auditable override rather than a silent default).
  3. Redaction -- nothing that looks like a credential, token, or full PII
     value is written into artifacts or logs; values are hashed/truncated.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .artifact import ActionType, AllowlistScope, RiskLevel


class GuardrailViolation(Exception):
    pass


@dataclass
class AllowlistPolicy:
    scope: AllowlistScope

    def check_url(self, url: str) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.hostname or ""
        domain_ok = any(host == d or host.endswith("." + d) for d in self.scope.allowed_domains)
        prefix_ok = (not self.scope.allowed_url_prefixes) or any(
            url.startswith(p) for p in self.scope.allowed_url_prefixes
        )
        if not (domain_ok and prefix_ok):
            raise GuardrailViolation(
                f"URL {url!r} is outside the allowlist "
                f"(domains={self.scope.allowed_domains}, prefixes={self.scope.allowed_url_prefixes})"
            )

    def check_action(self, action: ActionType) -> None:
        if action not in self.scope.allowed_action_types:
            raise GuardrailViolation(f"Action type {action} is not in the allowlist")


def check_risky_step(risk_level: RiskLevel, approval_state: str, allow_risky: bool) -> None:
    if risk_level == RiskLevel.RISKY and approval_state != "approved" and not allow_risky:
        raise GuardrailViolation(
            "Refusing to execute a RISKY (irreversible) step from a 'draft' artifact. "
            "Approve the artifact (approval_state='approved') or pass allow_risky=True "
            "explicitly to override."
        )


# --- Redaction -------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"(?i)password"),
    re.compile(r"(?i)passwd"),
    re.compile(r"(?i)token"),
    re.compile(r"(?i)secret"),
    re.compile(r"(?i)api[_-]?key"),
    re.compile(r"(?i)ssn"),
    re.compile(r"(?i)social.?security"),
]

# Loose PII-shaped patterns for values we might otherwise log verbatim.
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def is_sensitive_field_name(name: str) -> bool:
    return any(p.search(name) for p in _SECRET_PATTERNS)


def redact_value(value: str) -> str:
    """Irreversibly redact a value, keeping a short fingerprint for log
    correlation without exposing the underlying data."""
    if value is None:
        return value
    digest = hashlib.sha256(str(value).encode()).hexdigest()[:8]
    return f"<redacted:{digest}>"


def redact_text(text: str) -> str:
    if not text:
        return text
    text = _SSN_RE.sub(lambda m: redact_value(m.group()), text)
    text = _CARD_RE.sub(lambda m: redact_value(m.group()), text)
    return text


def redact_field(name: str, value):
    if isinstance(value, str) and (is_sensitive_field_name(name) or _SSN_RE.search(value) or _CARD_RE.search(value)):
        return redact_value(value)
    return value
