# Evidence index

**Read this first: the discovery-run evidence in this folder was NOT produced by a real
LLM.** I built and validated the entire system, but this sandbox had no Anthropic API
credentials available to me, so I could not personally execute the assignment's
non-negotiable requirement ("at least one genuine LLM-driven run against a live surface").
`discovery_scaffold_validation_SCRIPTED_NOT_REAL_LLM/` was produced with the
`scripted-test` provider (`src/cua/llm_provider.py::ScriptedTestProvider`) — a fixed,
hand-written action sequence that ignores the actual page state. It exists only to prove
the rest of the pipeline (artifact recording, locator resolution, checkpoints, guardrails,
evidence capture) works end to end. **It is not valid submission evidence on its own.**

To produce the real evidence before submitting, run:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python -m src.cua.cli discover --goal open_subaccount --member-id 12345 \
    --account-type "Youth Savings" --deposit 100 --provider anthropic
```

This drives a genuine `claude-sonnet-4-6` tool-calling loop against the live Flask app
(`src/target_app`) and writes a new `evidence/discovery_<timestamp>_<hash>/` directory
with the same structure as the scaffold run below — replace/augment this folder with
that output. See `/README.md` for full setup.

## What's here

| Directory | What it demonstrates |
|---|---|
| `discovery_scaffold_validation_SCRIPTED_NOT_REAL_LLM/` | Full discovery pipeline exercised end to end: all 11 steps recorded (zero action failures), credentials redacted to `{{OPERATOR_USERNAME}}`/`{{OPERATOR_PASSWORD}}` placeholders, only the true state-changing confirm step (`s9`) flagged `risky`, output field's `source_step` correctly backfilled to the extract step (`s10`). `run.jsonl` has the full step-by-step log; `artifact.json` is the resulting Capability. |
| `artifact_open_subaccount.json` | Canonical copy of that artifact, `approval_state` flipped to `"approved"` so replay can execute the RISKY confirm step. This is the file every replay demo below points at. |
| `replay_1_success/` | Deterministic replay against a **different** member (12345) than the artifact was recorded against (55555), proving it generalizes via `{{member_id}}`/`{{deposit}}`/`{{account_type}}` templating. Result: `SUCCESS`, `account_number` extracted correctly. |
| `replay_2_business_outcome_member_not_found/` | Replay against a nonexistent member ID. Result: `BUSINESS_OUTCOME` / `MEMBER_NOT_FOUND` — a clean, typed, non-crash outcome. |
| `replay_3_escalation_handoff_resume_SUCCESS/` | The human-in-the-loop path, run through the **actual `python -m src.cua.cli operator` command**, not just the underlying library. A session-timeout is deterministically injected (`--inject-session-timeout-before-step s4`), producing a genuine hard failure with no matching declared handler. The engine raises an `InterventionRequest` and blocks on `control.json`. The real operator CLI attaches to the **same live browser** over CDP (`http://127.0.0.1:9333`), re-authenticates, re-enters the form field lost when the session reset, and hands control back. Automation resumes on the same session and completes: `SUCCESS`. |
| `replay_4_business_outcome_permission_denied/` | Replay against a frozen member's account. Result: `BUSINESS_OUTCOME` / `PERMISSION_DENIED`. |
| `replay_5_business_outcome_validation_error/` | Replay with a deposit below the $25 minimum. Result: `BUSINESS_OUTCOME` / `VALIDATION_ERROR`. |
| `replay_6_recoverable_interstitial_dismissed_SUCCESS/` | Dedicated evidence for the third error classification (`RECOVERABLE`), isolated from the other two. `--inject-interstitial-before-step s2` deterministically forces the target app's transient "processing, please wait" interstitial via a test-only endpoint. The log shows the exact sequence: `fault_injected` → `error_handler_matched` (`transient_interstitial`, classification `recoverable`) → `recovered_continuing_next_step` → the run continues to `SUCCESS`. This is the one classification that isn't a terminal `ReplayResult`, so it's easy to miss in the other runs; this one isolates it explicitly. |

Every directory's `run.jsonl` is the structured, append-only event log for that run.
Screenshots are captured at key checkpoints and on any failure/escalation.

## Verified, not just claimed

Every one of these was re-run and checked mechanically before being included here:

- All 20 unit tests pass (`python -m pytest tests/ -v`).
- Every `.json`/`.jsonl` file in this directory parses without error.
- Every `.png` is a valid, non-corrupt image (spot-checked visually too).
- `grep -r "operator123" evidence/ --exclude="README.md"` returns **zero** matches — no
  literal credential anywhere in any artifact, log, or control file.
- All three discovery stopping conditions (Section 3.1: max-steps, timeout, dead-end/
  escalate) individually triggered and confirmed via direct unit-level tests, not just
  inferred from the CLI's normal-path behavior.
- The escalation demo above was driven through the literal `python -m src.cua.cli
  operator --evidence-dir ...` command a real user would type, not just the underlying
  Python classes.

## Bugs found and fixed during this verification pass (not hidden)

1. **Risk-classifier false positive.** A pure navigation link ("Open a new sub-account")
   was flagged `risky` because "open" matched a keyword list, blocking a draft artifact
   from reaching its own confirmation screen. Fixed: only `button`-role clicks are
   eligible for `RISKY`, never `link`-role navigation. Regression test added.
2. **A genuine credential leak.** The goal text originally embedded the literal password
   (persisted into `capability.description`), and `run.jsonl`'s `replay_started` event
   logged the raw `input_values` dict in plaintext (redaction only checked top-level
   string fields, not nested dicts). Found via a full-repo grep. Fixed at the source
   (credentials only ever exist in an ephemeral discovery-time system prompt) and at the
   logging layer (redaction now recurses into nested structures). Regression test added.
3. **`OutputField.source_step` was defined in the schema but never populated** — a
   field-by-field audit of a generated artifact caught it reading `null` when it should
   reference the extracting step. Fixed: `_build_capability` now backfills it from each
   step's `extract_as`. Regression test added.
4. **The discovery agent had no interstitial-recovery logic** (unlike the replay engine),
   which surfaced when an unrelated earlier test run's requests shared the target app's
   global "every 5th request" interstitial counter and tripped it mid-discovery,
   breaking the scripted stand-in provider (which — unlike a real LLM — can't adapt to
   an unexpected page). This is expected: `ScriptedTestProvider` is explicitly documented
   as blind to page state; a real LLM observing the interstitial would just click
   "Continue" itself. Rather than just re-running until it didn't collide, I added a
   proper fix: a test-only `/__test__/force_interstitial` endpoint plus a new
   `--inject-interstitial-before-step` replay flag, mirroring the existing session-timeout
   injection, so the `RECOVERABLE` path can be demonstrated deterministically instead of
   relying on chance (see `replay_6_recoverable_interstitial_dismissed_SUCCESS/` above).
