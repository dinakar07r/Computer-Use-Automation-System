# Computer-Use Automation System

A small, real implementation of the interface.ai take-home: an LLM discovers how to
accomplish a goal inside a legacy, no-API back-office web app; the successful run is
recorded as a typed, versioned **Capability** artifact; that artifact is then replayed
**deterministically, with no LLM in the loop**, with explicit error/business-outcome
handling and a real human-escalation handoff.

**Read `/evidence/README.md` before evaluating `/evidence/` — it explains which evidence
is a genuine LLM-driven run and which is a scaffold-validation stand-in, and gives the
exact command to produce the real one.** See `/REPORT.md` for the full design write-up.

## What's in here

```
src/target_app/     Mock legacy bank/credit-union back-office (Flask, server-rendered,
                     table layout, no data-testid) -- the "one concrete surface"
src/cua/            The system itself:
  artifact.py          Capability schema (the agent-invocable contract)
  perception.py        Accessibility-tree observation of the live page
  locator.py            Locator resolution with a fallback chain
  discovery_agent.py   LLM-driven observe/decide/act loop -> Capability
  llm_provider.py      Real Anthropic provider + an offline scripted stub
  replay_engine.py     Deterministic replay, error taxonomy, checkpoints
  guardrails.py        Allowlist, risky-step policy, redaction
  escalation.py        Human handoff / control-transfer state machine
  logging_utils.py     Structured JSONL evidence logging
  cli.py               discover / replay / operator commands
config/allowlist.yaml  Guardrail allowlist for the target app
evidence/            Saved runs -- see evidence/README.md
tests/                Unit tests (schema, guardrails, locator/templating logic)
```

## Setup

Requires Python 3.11+.

```bash
pip install --break-system-packages -r requirements.txt
python -m playwright install chromium
```

## Run without live services

The Capability schema, guardrail logic, and locator/templating helpers are pure Python
and covered by unit tests that need no browser, no Flask server, and no API key:

```bash
python -m pytest tests/ -v
```

## Demo path

**1. Start the target app** (a separate terminal, or background it):

```bash
python -m src.target_app.app
# -> http://127.0.0.1:5055  (operator / operator123)
```

**2. Run a real discovery session.** This requires your own Anthropic API key — the
assignment is explicit that a genuine LLM-driven run is not optional:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python -m src.cua.cli discover \
    --goal open_subaccount --member-id 12345 \
    --account-type "Youth Savings" --deposit 100 \
    --provider anthropic
```

This drives Chromium against the live app, records the successful run, and writes
`evidence/discovery_<timestamp>_<hash>/artifact.json` plus a structured `run.jsonl` log.

There's also a second, simpler goal template for a read-only flow:

```bash
python -m src.cua.cli discover --goal member_lookup --member-id 12345 --provider anthropic
```

**No API key available right now?** You can still exercise every other part of the
system (locators, guardrails, checkpoints, replay, escalation) with a scripted stand-in
that is *not* an LLM and is loudly labeled as such:

```bash
python -m src.cua.cli discover --goal open_subaccount --member-id 55555 \
    --account-type "Youth Savings" --deposit 100 --provider scripted-test
```

**3. Approve and replay the artifact deterministically** (no model calls):

```bash
python -m src.cua.cli replay \
    --artifact evidence/discovery_<...>/artifact.json \
    --param member_id=12345 --param account_type="Youth Savings" --param deposit=100
```

New artifacts start `approval_state: "draft"`; the RISKY confirm step is blocked unless
the artifact is approved or you pass `--allow-risky` explicitly (see REPORT.md Section 6).
To flip an artifact to approved: edit `"approval_state": "draft"` -> `"approved"` in the
JSON, or use `jq`.

Try it against a different member than the one used for discovery, and against a
business-outcome case:

```bash
python -m src.cua.cli replay --artifact <artifact.json> --param member_id=99999 \
    --param account_type="Youth Savings" --param deposit=100
# -> {"status": "BUSINESS_OUTCOME", "outcome_code": "MEMBER_NOT_FOUND", ...}
```

**4. See the human-escalation handoff.** This deterministically injects a session-timeout
right before a step (clears cookies), which the replay engine can't recover from on its
own, so it escalates:

```bash
python -m src.cua.cli replay --artifact <artifact.json> \
    --param member_id=12345 --param account_type="Youth Savings" --param deposit=100 \
    --enable-escalation --inject-session-timeout-before-step s4
```

This process blocks, waiting for a human. In a second terminal, run the operator
console (a minimal, mocked console — see REPORT.md Section 5 for what a full one would
add) to take over the **same live browser session** over CDP and hand control back:

```bash
python -m src.cua.cli operator --evidence-dir evidence/replay_<...>
```

Once it attaches, log back in through the browser (headless, but you can pass
`--no-headless` to `replay` to watch it), then type `resume`. The replay process picks
up on the same session and completes.

**5. See the RECOVERABLE path in isolation.** The target app occasionally shows a
transient "processing, please wait" interstitial (organically, every ~5th request, to
simulate a realistic runtime condition). To demonstrate the replay engine dismissing it
and continuing — rather than waiting for it to happen by chance — force it
deterministically:

```bash
python -m src.cua.cli replay --artifact <artifact.json> \
    --param member_id=12345 --param account_type="Youth Savings" --param deposit=100 \
    --inject-interstitial-before-step s2
# -> {"status": "SUCCESS", ...} after transparently dismissing the interstitial
```

## Before Running
 
**Run the real discovery command yourself**, with your own `ANTHROPIC_API_KEY`
(see the "Demo path" section above for the exact command). This produces the actual
`evidence/discovery_<timestamp>_<hash>/` directory with `artifact.json` and
`run.jsonl` from a genuine model-driven session — not the `scripted-test` stand-in.


## Notes

- `OPERATOR_USERNAME` / `OPERATOR_PASSWORD` are resolved from environment variables at
  replay time (defaulting to the demo app's `operator`/`operator123`), never stored in
  the artifact — see `guardrails.py` and REPORT.md Section 6.
- All evidence directories are self-contained under `evidence/<run_id>/`: `run.jsonl`
  (structured log), `artifact.json`, and screenshots at key checkpoints/failures.
