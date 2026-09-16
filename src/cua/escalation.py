"""
Human-in-the-loop escalation & handoff (assignment Section 3.6).

Design: automation and the human operator share the SAME live browser
session via the Chrome DevTools Protocol (CDP), not a fresh one. The
automation process launches the browser with a remote-debugging port; a
separate "operator" process (mocked as a small CLI in cli.py, per the scope
note in Section 4/8) connects to that exact same browser over CDP, acts on
the live page, and then hands control back.

Control transfer is modeled explicitly as a tiny file-backed state machine
(`control.json` in the run's evidence directory) rather than an implicit
convention:

    {"owner": "automation" | "human", "pending_request": {...} | null,
     "resume_signal": bool, "human_actions": [...]}

- Automation calls `request_human()` when it hits a condition it can't
  safely resolve (stuck during discovery, a hard failure during replay, or a
  RISKY step that needs a person). This writes an IntervationRequest with
  enough context to act on (goal/capability, step, screenshot, reason),
  flips `owner` to "human", and blocks polling `control.json`.
- The operator CLI attaches over CDP to the same page, performs whatever
  manual steps are needed, logs each one, then calls `hand_back()`.
- Automation observes `owner == "automation"` again and resumes -- for
  replay this means re-checking the current page state and continuing with
  the next step; for discovery the run is marked ESCALATED and stopped
  (see /REPORT.md section 5 for why full mid-reasoning resume for the LLM
  loop is out of scope here, and what a full implementation would add).

A full real-time co-browsing console is explicitly out of scope (assignment
Section 3.6 scope note); what's real here is the session-sharing mechanism
and the control-transfer contract, which is the load-bearing part.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class InterventionRequest:
    run_id: str
    capability_name: str
    goal: str
    step_context: str
    reason: str
    screenshot_path: str
    cdp_endpoint: str
    created_at: float = field(default_factory=time.time)


class ControlChannel:
    """File-backed control-transfer state machine scoped to one evidence dir."""

    def __init__(self, evidence_dir: str | Path):
        self.dir = Path(evidence_dir)
        self.path = self.dir / "control.json"
        if not self.path.exists():
            self._write({"owner": "automation", "pending_request": None,
                         "resume_signal": False, "human_actions": []})

    def _read(self) -> dict:
        return json.loads(self.path.read_text())

    def _write(self, state: dict) -> None:
        self.path.write_text(json.dumps(state, indent=2))

    # -- automation side --------------------------------------------------

    def request_human(self, req: InterventionRequest) -> None:
        state = self._read()
        state["owner"] = "human"
        state["resume_signal"] = False
        state["pending_request"] = {
            "run_id": req.run_id,
            "capability_name": req.capability_name,
            "goal": req.goal,
            "step_context": req.step_context,
            "reason": req.reason,
            "screenshot_path": req.screenshot_path,
            "cdp_endpoint": req.cdp_endpoint,
            "created_at": req.created_at,
        }
        self._write(state)
        (self.dir / "intervention_request.json").write_text(
            json.dumps(state["pending_request"], indent=2)
        )

    def wait_for_human(self, poll_interval: float = 1.0, timeout: float = 600.0) -> list[dict]:
        """Blocks until the operator hands control back. Returns the log of
        human actions taken during the handoff."""
        start = time.time()
        while time.time() - start < timeout:
            state = self._read()
            if state["owner"] == "automation" and state["resume_signal"]:
                return state.get("human_actions", [])
            time.sleep(poll_interval)
        raise TimeoutError("Timed out waiting for human operator to hand control back")

    # -- operator side ------------------------------------------------------

    def is_pending(self) -> bool:
        return self._read()["owner"] == "human"

    def get_pending_request(self) -> dict | None:
        return self._read().get("pending_request")

    def record_human_action(self, description: str) -> None:
        state = self._read()
        state.setdefault("human_actions", []).append(
            {"ts": time.time(), "action": description}
        )
        self._write(state)

    def hand_back(self) -> None:
        state = self._read()
        state["owner"] = "automation"
        state["resume_signal"] = True
        self._write(state)
