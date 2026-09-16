"""
Structured, append-only JSONL logging for every run (discovery or replay),
plus a richer failure signal (screenshot) -- assignment Section 3.5.

Each run gets its own evidence directory:
    evidence/<run_id>/run.jsonl        - one JSON object per event
    evidence/<run_id>/artifact.json    - the artifact (discovery output, or the one being replayed)
    evidence/<run_id>/screenshot_*.png - captured on failure / escalation / key checkpoints
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .guardrails import redact_field, redact_text


def _deep_redact(obj):
    """Recursively redact sensitive fields inside nested dicts/lists. The
    shallow, top-level-only redaction in the original implementation missed
    exactly the case that matters most: `input_values` dicts (e.g. containing
    OPERATOR_PASSWORD) passed as a single field to `event()`. Caught by
    testing -- see /REPORT.md Section 6."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str):
                out[k] = redact_field(k, v)
            else:
                out[k] = _deep_redact(v)
        return out
    if isinstance(obj, list):
        return [_deep_redact(v) for v in obj]
    return obj


class EvidenceLogger:
    def __init__(self, evidence_root: str, run_kind: str, run_id: str | None = None):
        self.run_id = run_id or f"{run_kind}_{time.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.dir = Path(evidence_root) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self.dir / "run.jsonl"
        self._fh = open(self._log_path, "a", encoding="utf-8")
        self._shot_count = 0
        self.event("run_started", kind=run_kind, run_id=self.run_id)

    def event(self, event_type: str, **fields):
        safe_fields = _deep_redact(fields)
        if "message" in safe_fields and isinstance(safe_fields["message"], str):
            safe_fields["message"] = redact_text(safe_fields["message"])
        record = {
            "ts": time.time(),
            "event": event_type,
            **safe_fields,
        }
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()

    def screenshot(self, page, label: str) -> str:
        self._shot_count += 1
        fname = f"screenshot_{self._shot_count:02d}_{label}.png"
        path = self.dir / fname
        try:
            page.screenshot(path=str(path))
        except Exception as e:  # noqa: BLE001
            self.event("screenshot_failed", label=label, error=str(e))
            return ""
        self.event("screenshot_captured", label=label, path=str(path))
        return str(path)

    def save_artifact(self, artifact_json: str):
        (self.dir / "artifact.json").write_text(artifact_json, encoding="utf-8")

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc:
            self.event("run_crashed", error=str(exc), error_type=str(exc_type))
        self.close()
