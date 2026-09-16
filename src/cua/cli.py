"""
CLI entrypoints.

    python -m src.cua.cli discover   --goal open_subaccount --member-id 12345 ...
    python -m src.cua.cli replay     --artifact evidence/.../artifact.json --member-id 12345 ...
    python -m src.cua.cli operator   --evidence-dir evidence/<run_id>

See /README.md for full usage and exact demo commands.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

from .artifact import AllowlistScope, Capability, InputParam, OutputField
from .discovery_agent import DiscoveryAgent, DiscoveryError, EscalationRequested
from .escalation import ControlChannel
from .llm_provider import ScriptedTestProvider, build_provider
from .logging_utils import EvidenceLogger
from .replay_engine import ReplayEngine

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = str(REPO_ROOT / "evidence")
CDP_PORT = 9333


def load_allowlist(path: str = str(REPO_ROOT / "config" / "allowlist.yaml")) -> AllowlistScope:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return AllowlistScope(**raw)


def _launch_browser(p, headless: bool = True):
    browser = p.chromium.launch(headless=headless, args=[f"--remote-debugging-port={CDP_PORT}"])
    context = browser.new_context()
    page = context.new_page()
    cdp_endpoint = f"http://127.0.0.1:{CDP_PORT}"
    return browser, page, cdp_endpoint


# ---------------------------------------------------------------------------
# Goal templates -- the two demo capabilities used throughout README/REPORT.
# ---------------------------------------------------------------------------

def _goal_open_subaccount(member_id: str, account_type: str, deposit: str, base_url: str):
    # NOTE: credentials are deliberately NOT embedded in this goal text. This
    # string becomes `capability.description`, which IS persisted into the
    # artifact and into evidence logs -- see the redaction fix in
    # logging_utils.py and guardrails.py. Credentials are passed to the agent
    # separately (see `credentials=` below) and only ever touch an in-memory
    # system prompt, never a persisted field.
    goal = (
        f"Log in as the operator using the credentials you've been given out of band. "
        f"Then look up member {member_id}, open a new sub-account of type "
        f"'{account_type}' with an initial deposit of ${deposit}, and reach the "
        f"confirmation/result screen showing the new account number."
    )
    return dict(
        goal=goal,
        initial_url=f"{base_url}/login",
        input_values={"member_id": member_id, "account_type": account_type, "deposit": deposit},
        input_param_specs=[
            InputParam(name="member_id", type="string", description="Member ID to open the sub-account for"),
            InputParam(name="account_type", type="string", description="Sub-account type label as shown in the dropdown"),
            InputParam(name="deposit", type="number", description="Initial deposit amount in USD"),
        ],
        output_field_specs=[
            OutputField(name="account_number", type="string", description="Newly created sub-account number"),
        ],
        capability_name="open_subaccount",
    )


def _goal_member_lookup(member_id: str, base_url: str):
    goal = (
        f"Log in as the operator using the credentials you've been given out of band. "
        f"Then look up member {member_id} and read their current savings balance."
    )
    return dict(
        goal=goal,
        initial_url=f"{base_url}/login",
        input_values={"member_id": member_id},
        input_param_specs=[
            InputParam(name="member_id", type="string", description="Member ID to look up"),
        ],
        output_field_specs=[
            OutputField(name="savings_balance", type="string", description="Member's current savings balance"),
        ],
        capability_name="member_savings_lookup",
    )


GOAL_TEMPLATES = {
    "open_subaccount": _goal_open_subaccount,
    "member_lookup": _goal_member_lookup,
}


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

def cmd_discover(args):
    allowlist = load_allowlist()
    base_url = args.base_url

    if args.goal == "open_subaccount":
        cfg = _goal_open_subaccount(args.member_id, args.account_type, args.deposit, base_url)
    elif args.goal == "member_lookup":
        cfg = _goal_member_lookup(args.member_id, base_url)
    else:
        raise SystemExit(f"Unknown --goal {args.goal!r}")

    evidence = EvidenceLogger(EVIDENCE_ROOT, run_kind="discovery")
    print(f"[discover] evidence dir: {evidence.dir}")

    if args.provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: --provider anthropic requires ANTHROPIC_API_KEY to be set in the environment.",
              file=sys.stderr)
        sys.exit(2)

    if args.provider == "scripted-test":
        script = _scripted_script_for(args.goal, cfg)
        provider = ScriptedTestProvider(script=script)
        print("WARNING: using scripted-test provider -- NOT a real LLM. This run's evidence "
              "does NOT satisfy the assignment's 'genuine discovery run' requirement. "
              "Use --provider anthropic with ANTHROPIC_API_KEY set for the real run.")
    else:
        provider = build_provider("anthropic", model=args.model)

    with sync_playwright() as p:
        browser, page, cdp_endpoint = _launch_browser(p, headless=args.headless)
        try:
            agent = DiscoveryAgent(
                page=page, provider=provider, allowlist=allowlist, evidence=evidence,
                target_app_id="legacy_bank_console", base_url=base_url,
                surface_type="legacy_web", max_steps=args.max_steps, timeout_s=args.timeout,
            )
            try:
                capability = agent.run(
                    goal=cfg["goal"], initial_url=cfg["initial_url"],
                    input_values=cfg["input_values"], input_param_specs=cfg["input_param_specs"],
                    output_field_specs=cfg["output_field_specs"], capability_name=cfg["capability_name"],
                    credentials={
                        "username": os.environ.get("OPERATOR_USERNAME", "operator"),
                        "password": os.environ.get("OPERATOR_PASSWORD", "operator123"),
                    },
                )
            except EscalationRequested as e:
                print(f"[discover] agent escalated: {e.reason}")
                evidence.event("discovery_ended_escalated", reason=e.reason)
                sys.exit(3)
            except DiscoveryError as e:
                print(f"[discover] discovery failed: {e}")
                evidence.event("discovery_ended_failed", error=str(e))
                sys.exit(4)

            artifact_json = capability.model_dump_json_pretty()
            evidence.save_artifact(artifact_json)
            out_path = Path(args.out) if args.out else (evidence.dir / "artifact.json")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(artifact_json)
            print(f"[discover] SUCCESS. Capability saved to {out_path}")
            print(f"[discover] evidence: {evidence.dir}")
        finally:
            evidence.close()
            browser.close()


def _scripted_script_for(goal: str, cfg: dict) -> list[dict]:
    """Fixed action sequence for the offline scaffold-validation run. Ignores
    the observation; used only to prove the harness works without API
    credentials. NOT valid discovery-run evidence for submission."""
    iv = cfg["input_values"]
    if goal == "open_subaccount":
        return [
            {"tool": "type_text", "input": {"role": "textbox", "name": "Username", "text": "operator", "reasoning": "log in"}},
            {"tool": "type_text", "input": {"role": "textbox", "name": "Password", "text": "operator123", "reasoning": "log in"}},
            {"tool": "click", "input": {"role": "button", "name": "Log In", "reasoning": "submit login"}},
            {"tool": "type_text", "input": {"role": "textbox", "name": "Member ID", "text": iv["member_id"], "reasoning": "search member"}},
            {"tool": "click", "input": {"role": "button", "name": "Search", "reasoning": "submit search"}},
            {"tool": "click", "input": {"role": "link", "name": "Open a new sub-account", "reasoning": "start subaccount flow"}},
            {"tool": "select_option", "input": {"role": "combobox", "name": "Account Type", "option_label": cfg["input_values"]["account_type"], "reasoning": "choose type"}},
            {"tool": "type_text", "input": {"role": "textbox", "name": "Initial Deposit ($)", "text": iv["deposit"], "reasoning": "enter deposit"}},
            {"tool": "click", "input": {"role": "button", "name": "Continue", "reasoning": "go to confirm screen"}},
            {"tool": "click", "input": {"role": "button", "name": "Confirm and Open Account", "reasoning": "confirm"}},
            {"tool": "extract", "input": {"row_label": "Account Number", "output_name": "account_number", "reasoning": "capture new account number"}},
            {"tool": "done", "input": {"checkpoint_text": "Sub-Account Opened", "reasoning": "result screen shown"}},
        ]
    return [
        {"tool": "type_text", "input": {"role": "textbox", "name": "Username", "text": "operator", "reasoning": "log in"}},
        {"tool": "type_text", "input": {"role": "textbox", "name": "Password", "text": "operator123", "reasoning": "log in"}},
        {"tool": "click", "input": {"role": "button", "name": "Log In", "reasoning": "submit login"}},
        {"tool": "type_text", "input": {"role": "textbox", "name": "Member ID", "text": iv["member_id"], "reasoning": "search member"}},
        {"tool": "click", "input": {"role": "button", "name": "Search", "reasoning": "submit search"}},
        {"tool": "extract", "input": {"row_label": "Savings Balance", "output_name": "savings_balance", "reasoning": "capture balance"}},
        {"tool": "done", "input": {"checkpoint_text": "Member Profile", "reasoning": "profile shown"}},
    ]


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def cmd_replay(args):
    capability = Capability.model_validate_json(Path(args.artifact).read_text())
    input_values = dict(item.split("=", 1) for item in (args.param or []))
    input_values.setdefault("OPERATOR_USERNAME", os.environ.get("OPERATOR_USERNAME", "operator"))
    input_values.setdefault("OPERATOR_PASSWORD", os.environ.get("OPERATOR_PASSWORD", "operator123"))

    evidence = EvidenceLogger(EVIDENCE_ROOT, run_kind="replay")
    evidence.save_artifact(capability.model_dump_json_pretty())
    print(f"[replay] evidence dir: {evidence.dir}")

    control = ControlChannel(evidence.dir) if args.enable_escalation else None

    with sync_playwright() as p:
        browser, page, cdp_endpoint = _launch_browser(p, headless=args.headless)
        try:
            engine = ReplayEngine(page=page, evidence=evidence, control=control, cdp_endpoint=cdp_endpoint)
            result = engine.replay(
                capability, input_values, allow_risky=args.allow_risky,
                inject_session_timeout_before_step=args.inject_session_timeout_before_step,
                inject_interstitial_before_step=args.inject_interstitial_before_step,
            )
            print(json.dumps(result.model_dump(), indent=2, default=str))
        finally:
            evidence.close()
            browser.close()


# ---------------------------------------------------------------------------
# operator (mock intervention console)
# ---------------------------------------------------------------------------

def cmd_operator(args):
    control = ControlChannel(args.evidence_dir)
    print("Polling for a pending intervention request... (Ctrl+C to stop)")
    while True:
        if control.is_pending():
            break
        time.sleep(1.0)

    req = control.get_pending_request()
    print("\n=== INTERVENTION REQUEST ===")
    print(json.dumps(req, indent=2))
    print("============================\n")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(req["cdp_endpoint"])
        context = browser.contexts[0]
        page = context.pages[0]
        print(f"Attached to the SAME live session. Current URL: {page.url}")
        print("Commands: click <role> <name...> | type <role> <name...> :: <text> | screenshot | resume")
        while True:
            try:
                cmd = input("operator> ").strip()
            except EOFError:
                cmd = "resume"
            if not cmd:
                continue
            if cmd == "resume":
                control.hand_back()
                print("Handed control back to automation.")
                break
            if cmd == "screenshot":
                path = f"{args.evidence_dir}/operator_screenshot_{int(time.time())}.png"
                page.screenshot(path=path)
                print(f"saved {path}")
                continue
            try:
                if cmd.startswith("click "):
                    _, role, name = cmd.split(" ", 2)
                    page.get_by_role(role, name=name).first.click()
                    control.record_human_action(f"click role={role} name={name}")
                    print("ok")
                elif cmd.startswith("type "):
                    rest = cmd[len("type "):]
                    target, text = rest.split("::", 1)
                    role, name = target.strip().split(" ", 1)
                    page.get_by_role(role, name=name.strip()).first.fill(text.strip())
                    control.record_human_action(f"type role={role} name={name.strip()} text=<entered>")
                    print("ok")
                else:
                    print("unrecognized command")
            except Exception as e:  # noqa: BLE001
                print(f"error: {e}")
        browser.close()


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(prog="cua")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="Run an LLM-driven discovery session and save a Capability artifact")
    d.add_argument("--goal", choices=list(GOAL_TEMPLATES.keys()), default="open_subaccount")
    d.add_argument("--member-id", default="12345")
    d.add_argument("--account-type", default="Youth Savings")
    d.add_argument("--deposit", default="100")
    d.add_argument("--base-url", default="http://127.0.0.1:5055")
    d.add_argument("--provider", choices=["anthropic", "scripted-test"], default="anthropic")
    d.add_argument("--model", default="claude-sonnet-4-6")
    d.add_argument("--max-steps", type=int, default=20)
    d.add_argument("--timeout", type=int, default=180)
    d.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    d.add_argument("--out", default=None)
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("replay", help="Deterministically replay a saved Capability artifact")
    r.add_argument("--artifact", required=True)
    r.add_argument("--param", action="append", help="key=value, repeatable")
    r.add_argument("--allow-risky", action="store_true")
    r.add_argument("--enable-escalation", action="store_true",
                    help="Wire up the human-escalation control channel for this run")
    r.add_argument("--inject-session-timeout-before-step", default=None,
                    help="Step id before which to clear session cookies, to demonstrate hard-failure escalation")
    r.add_argument("--inject-interstitial-before-step", default=None,
                    help="Step id before which to deterministically force the transient interstitial, "
                         "to demonstrate RECOVERABLE-path handling")
    r.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    r.set_defaults(func=cmd_replay)

    o = sub.add_parser("operator", help="Mock operator console: take over the live session on escalation")
    o.add_argument("--evidence-dir", required=True)
    o.set_defaults(func=cmd_operator)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
