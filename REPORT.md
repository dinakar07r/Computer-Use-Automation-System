# REPORT

## 1. Architecture

Single process, three layers, one shared contract between them (the artifact). No
queues, no services, no multi-tenant plumbing — the brief explicitly says not to build
that infrastructure prematurely, so I didn't.

```
LLM (discovery only) --tool calls--> DiscoveryAgent --records--> Capability (artifact)
                                            |                          |
                                       guardrails.py              replay_engine.py
                                            |                          |
                                       Playwright page  <--drives--  (no LLM)
```

- **Perception is accessibility-tree based, not raw DOM or screenshots**
  (`perception.py`). I bias toward this because it's the one channel that's explicitly
  called out as working on both legacy web markup and desktop apps (Section 1's
  glossary), and because it's what a human operator actually perceives — role +
  accessible name — rather than incidental markup structure.
- **The LLM never touches the browser directly.** Every tool call it makes
  (`click`/`type_text`/`select_option`/`navigate`/`extract`/`done`/`escalate`) is
  executed through the same locator-resolution and guardrail layer that replay uses.
  What the agent actually did during discovery *is* the artifact — there's no separate
  "compile the transcript into steps" pass to get wrong.
- **Replay shares almost no code path with discovery except the locator resolver and
  guardrails.** Discovery's job is producing a good artifact; replay's job is executing
  one fast and predictably. Conflating them (e.g., letting replay fall back to an LLM
  by default) would undermine the "no model in the decision loop" requirement, so the
  two are cleanly separated and only reconnect at the escalation boundary (Section 5).
- **Target surface**: a small Flask app I built to stand in for a real core-banking
  console — server-rendered, table-based layout, no `data-testid`, no clean CSS hooks,
  but semantically correct (`<label for>`, `<button>`, `<select>`) the way a real
  (if dated) enterprise form would be. It intentionally reproduces three of the
  runtime conditions Section 1 calls out: a "member not found" business outcome, a
  frozen-account permission denial, and a transient "processing, please wait"
  interstitial that appears on roughly 1 in 5 requests.

**Trade-off I made and would revisit with more time:** everything runs in one Python
process per CLI invocation (discover, or replay). That's the right scope for "small but
real" — see Section 4 for how I'd actually scale this.

## 2. Artifact schema

`artifact.py`'s `Capability` is the focal point, so I'll explain the shape rather than
just list fields (full schema in the file itself):

- **`steps: list[Step]`** — each step is one action, with a `LocatorSpec` (not a bare
  selector string): a `primary` locator strategy plus an ordered `fallbacks` list, and a
  human-readable `robustness_note` explaining *why* the strategy should survive minor
  drift. This is deliberate: a single selector string can't express "try role+name
  first, fall back to text-match" or explain the reasoning to a reviewer. During
  discovery, role+accessible-name is preferred (`locator.spec_for_role`) precisely
  because it's stable across markup/CSS changes, which is the failure mode the brief
  says matters (not constant drift, but runtime conditions on top of a stable UI).
- **`input_params` / `output_fields`** are typed and named separately from the steps
  that use them — a step's `value` field just contains a `{{param_name}}` placeholder.
  This is what makes an artifact a genuine callable capability (`open_subaccount(member_id,
  account_type, deposit) -> {account_number}`) rather than a fixed macro. Concrete
  values seen during discovery are template-substituted back to placeholders before
  the artifact is saved (`discovery_agent._templatize`) — including inside recorded
  **checkpoints**, which was a real bug I caught by testing: a checkpoint like
  `url_contains: /members/55555` only proves the *one* member used during recording
  reached that page; only `/members/{{member_id}}` proves the *capability* works.
- **Credentials are never captured as literal values.** When the discovery agent types
  into a field named "Username"/"Password" it records `{{OPERATOR_USERNAME}}` /
  `{{OPERATOR_PASSWORD}}` instead of the literal text, and the literal text is redacted
  from the evidence log before it's ever written to disk (not just before saving the
  artifact). Replay resolves those placeholders from environment variables — a stand-in
  for a real secret store.
- **`error_handlers`** are part of the artifact, not the executor, so a reviewer can see
  the full "what runtime conditions does this capability know about" story in one
  place, and so a different app's artifact can carry a different set without code
  changes.
- **`approval_state: draft | approved`** plus per-step `risk_level` gates whether RISKY
  steps can execute at all (Section 6). New artifacts are `draft` by construction.
- **`schema_version`** (artifact format) is separate from **`version`** (this specific
  capability's revision) — re-recording bumps the latter without needing to touch the
  former.

## 3. Determinism & error handling

Replay (`replay_engine.py`) never asks a model anything. Determinism comes from three
things: (a) locator resolution always tries the same ordered strategy list, (b) every
step that matters has a **checkpoint** asserted immediately after it — not "assume the
click worked," actually check the URL/text/element state — and (c) runtime conditions
are classified against the artifact's declared `error_handlers`, not inferred ad hoc.

The three-way split the brief asks for is enforced structurally, not just as naming
convention:

- **`BUSINESS_OUTCOME`** (e.g. `MEMBER_NOT_FOUND`, `PERMISSION_DENIED`,
  `VALIDATION_ERROR`) is a terminal `ReplayResult` with a stable `outcome_code` — the
  calling agent gets a normal, typed answer, not an exception.
- **`RECOVERABLE`** (the transient interstitial) triggers a bounded, declared recovery
  action (`dismiss_and_retry`, capped at `max_recovery_attempts`) and then **either**
  retries the step that was blocked **or** — if the step itself already succeeded and
  the interstitial just got in the way of observing that — continues to the next step
  instead. I initially had this collapsed into one path and it produced a real bug:
  the engine re-clicked "Log In" *after* login had already succeeded, because an
  interstitial appeared on the page the click had correctly navigated to. Distinguishing
  "recoverable-before-a-step-completes" from "recoverable-after-a-step-succeeds" fixed
  it — see `_run_step` vs `_handle_bad_state` calling `_apply_handler` with different
  `retry_step` values.
- **`HARD_FAILURE`** (no declared handler matches) escalates to a human if a control
  channel is wired up, otherwise fails fast with `failed_step_id` / `expected` /
  `observed` — enough to debug without re-running.

All three classes now have dedicated, isolated evidence rather than being inferred from
side effects of other runs: `/evidence/replay_2_business_outcome_member_not_found/`
(BUSINESS_OUTCOME), `/evidence/replay_6_recoverable_interstitial_dismissed_SUCCESS/`
(RECOVERABLE), and the escalation run in Section 5 (HARD_FAILURE). The RECOVERABLE demo
uses a new `--inject-interstitial-before-step` replay flag and a test-only
`/__test__/force_interstitial` endpoint (mirroring the existing session-timeout
injection) so it's reproducible on demand rather than depending on the target app's
organic every-5th-request trigger — which is realistic but, as I found during a
compliance pass, made an unrelated earlier test run's request count trip the interstitial
mid-*discovery*, where there's no recovery logic at all (only replay has it). That's
expected, not a bug: `ScriptedTestProvider` is explicitly blind to page state, so it
can't adapt the way a real LLM would by just clicking "Continue" itself when it observes
something unexpected. The fix was adding a deterministic trigger for evidence generation,
not adding interstitial-recovery to discovery (a real LLM already handles it for free by
observing and reacting each turn).

**A limitation I found and am documenting rather than hiding**: resume-after-escalation
currently retries only the *failed step*, not earlier steps whose effects were lost by
whatever caused the escalation. In my session-timeout demo, clearing cookies also reset
the in-progress search form; the human operator has to notice and manually re-enter it
(shown in the evidence). A fuller implementation would track a "transaction boundary" —
the last step whose effect is durable server-side (e.g., logged in) — and resume from
there, replaying the intervening steps, rather than assuming the immediately-prior
step's form state survived. I cut this because it requires the artifact to express
step *grouping*, which is a real schema extension, not a quick fix.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is exactly the locator/perception boundary already in
the code: `perception.py` turns a live surface into a compact tree, and `locator.py`
resolves a `LocatorSpec` against it. Both are Playwright-specific today. A legacy-web
adapter needs no schema change at all — `LocatorStrategy.ROW_LABEL` (added when I hit a
real legacy table with no stable per-cell identifiers) is already the kind of
markup-shape-aware strategy that generalizes to frameset/table layouts. A desktop
adapter would implement the same two functions (`snapshot`/`render_for_llm`,
`resolve`) against the OS accessibility API (UIA/AT-SPI) instead of the browser
accessibility tree — `LocatorStrategy.ROLE` maps almost directly (role + name is a
concept both worlds share); only the underlying `_resolve_one` implementation changes.
Nothing above that seam — `artifact.py`, `discovery_agent.py`, `replay_engine.py` —
needs to know which surface it's talking to.

**Multi-tenant reuse.** Today `base_url` and `allowlist_scope` are baked into one
`Capability`. For hundreds of tenants running the same vendor product, I'd split that
into a **base capability** (steps, locators, schema — the vendor-product-level
know-how) plus a **tenant binding** (base_url, allowlist scope, and an optional list of
per-step overrides keyed by `step_id`, e.g. "tenant X's build renames the Confirm
button"). Replay would resolve `base ∘ override` at invocation time rather than the
artifact being re-recorded per tenant. This is a natural extension of the existing
`LocatorSpec.fallbacks` mechanism — a tenant override is just "prepend a
tenant-specific locator to the fallback chain" — so it doesn't require restructuring
`Step`, only adding a binding layer above `Capability`. **Drift detection**: since
replay already asserts a checkpoint after every step, per-tenant replay success/failure
rates are a free signal — a tenant whose checkpoint pass rate for a given step drops is
exactly the tenant whose vendor-product configuration has drifted from the base
recording, and that's the stretch-goal "confidence & approval" scoring mentioned in
Section 8, which I didn't build but the checkpoint data already supports.

## 5. Escalation & handoff

Automation and the human share **the same live browser session** via CDP, not a fresh
one — the replay process launches Chromium with `--remote-debugging-port`, and the
operator (a separate process, `cli.py`'s `operator` command) connects to that exact
endpoint and acts on the same `page` object's live DOM.

Control transfer is an explicit file-backed state machine (`escalation.py:
ControlChannel`), not an implicit convention: `owner: "automation" | "human"`, a
`pending_request` carrying full context (goal, capability name, step, reason,
screenshot path, CDP endpoint), a `resume_signal`, and a `human_actions` log the
operator appends to. Automation calls `request_human()` and blocks polling; the operator
calls `hand_back()`; automation resumes.

I demonstrated this concretely rather than describing it: `--inject-session-timeout-
before-step` deterministically clears cookies, the replay hits a checkpoint mismatch
with no matching `error_handler` (genuine hard failure), escalates, and blocks. A
second process attaches over CDP, sees the exact page the automation was stuck on
(`/login` — the app's real redirect-on-unauthenticated behavior), manually re-
authenticates, and hands back. The automation resumes and completes
(`evidence/replay_3_escalation_handoff_resume_SUCCESS/`).

What's mocked, deliberately: the operator "console" is a CLI, not a UI (scope note in
Section 3.6 explicitly allows this). What's real: the session-sharing mechanism, the
control-transfer contract, and the resume behavior. A full implementation would add (a)
a proper web UI over the same `ControlChannel` primitives, (b) resuming discovery
(currently an LLM escalation just stops the run — see Section 3's cut — rather than
resuming the LLM's reasoning mid-conversation, which is a much harder problem than
resuming deterministic replay), and (c) the transaction-boundary tracking noted above.

## 6. Safety

Three independent mechanisms (`guardrails.py`), checked at different points:

- **Allowlist** (`config/allowlist.yaml`): domain + URL-prefix + action-type allowlist,
  enforced on every `navigate` and before every action, both during discovery
  (`DiscoveryAgent` checks before executing any LLM-issued action) and replay
  (`ReplayEngine` checks before every step). An out-of-scope URL or action type raises
  `GuardrailViolation` and stops the run — there's no silent skip.
- **Risky-step policy**: every step carries `risk_level: safe | risky`, assigned
  heuristically during discovery by keyword (confirm/submit/create/open/delete/
  approve/save in the target element's name — see `classify_risk`). RISKY steps are
  blocked at replay time unless the artifact's `approval_state == "approved"` or the
  caller passes `--allow-risky` explicitly — an auditable, deliberate override rather
  than a default. New artifacts are `draft` by construction, so a freshly-discovered
  capability can't execute its irreversible step unattended until someone (or some
  approval process) reviews and approves it.
- **Redaction**: credential values are never captured as literals in the first place
  (Section 2), and a belt-and-suspenders text/field-name scanner (`redact_field`,
  `redact_text`) catches SSN-and-card-shaped strings and password/token/secret-named
  fields before anything is written to the evidence log, independent of whether the
  artifact-level redaction caught it.

**Limits, honestly**: the risk classifier is a keyword heuristic on the accessible
name, not a semantic understanding of consequence — a button labeled "Go" that happens
to be destructive wouldn't be flagged. A production version would want the LLM to
explicitly reason about and declare risk during discovery (there's a natural extension
point: add a `risk_assessment` field to the tool schema) rather than inferring it after
the fact from a keyword list. I also caught and fixed a real instance of the keyword
heuristic over-firing during compliance testing: it originally flagged CLICK actions by
name alone, so a plain navigation link ("Open a new sub-account") was blocked as RISKY
purely because "open" matched the keyword list — even though clicking it has no side
effects. Fixed by only considering `button`-role clicks (state-changing submissions)
eligible for RISKY status, never `link`-role navigation. Separately, a full grep of
generated evidence for the literal demo password turned up two real leaks — the artifact's
persisted `description` field and a nested `input_values` dict in the replay log that the
top-level-only redaction logic missed — both fixed (credentials now only ever exist in an
ephemeral discovery-time system prompt; redaction recurses into nested structures). See
`evidence/README.md` for the full writeup; both are exactly the class of bug this
guardrail layer exists to catch, which is why I'm calling them out rather than quietly
fixing and moving on.

## 7. Cuts

What I deliberately left thin, and why:

- **Discovery-side escalation resume.** When the LLM calls `escalate`, the run stops
  (`EscalationRequested`) rather than pausing/resuming — resuming mid-reasoning would
  need conversation-state serialization and a real answer to "does the human's fix
  change what the model should try next," which felt like a distinct, harder feature
  rather than a corner of this one. The `ControlChannel` primitive is shared and ready
  for it.
- **Full step-group resume after escalation** (Section 3) — resume currently retries
  only the failed step; multi-step form state lost by whatever caused the escalation
  has to be manually restored by the operator, as shown in the evidence.
- **Multi-tenant binding layer** (Section 4) — designed, not built, per the brief's own
  instruction not to build scaling infrastructure prematurely.
- **Operator UI** — CLI only, per the explicit scope note allowing a mocked console.
- **Stretch goals** — I didn't build any (agent-facing capability catalog, code-gen,
  confidence scoring, assisted fallback, multi-run stability) to keep the core loop's
  depth (schema, replay/error-handling, escalation, safety) solid rather than spread
  thin, per Section 5/8's explicit "depth over breadth" guidance.

**What I'd build next with more time**: the tenant-binding layer (Section 4) and
step-group resume (Section 3) first, since both extend the existing schema/state-machine
rather than requiring new architecture; then a real risk-assessment tool call during
discovery (Section 6) instead of the keyword heuristic.
