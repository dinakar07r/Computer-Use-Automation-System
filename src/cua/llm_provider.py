"""
LLM provider abstraction for the discovery agent's decide step.

AnthropicProvider is the real, production-shaped integration: it calls the
Claude Messages API with tool-calling, feeding the accessibility-tree
observation each turn and getting back one tool call (click/type/navigate/
extract/done/escalate) at a time. This is what a genuine discovery run uses.

ScriptedTestProvider is NOT an LLM. It replays a fixed, hand-written sequence
of actions regardless of what it observes. It exists purely so the rest of
the system (artifact recording, replay, guardrails, escalation, evidence
capture) can be exercised and unit-tested without API credentials or network
access. It is loud about what it is, and /REPORT.md and /README.md are
explicit that it must never be used to produce the submission's required
discovery-run evidence -- only a real AnthropicProvider run counts for that.
"""
from __future__ import annotations

import abc
import json
import os
from dataclasses import dataclass


TOOLS = [
    {
        "name": "click",
        "description": "Click a control on the page, identified by its accessible role and visible/accessible name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "ARIA/accessibility role, e.g. button, link, textbox"},
                "name": {"type": "string", "description": "Visible/accessible name of the element"},
                "reasoning": {"type": "string"},
            },
            "required": ["role", "name", "reasoning"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into an input/textbox identified by role+name (or label).",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "text": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["role", "name", "text", "reasoning"],
        },
    },
    {
        "name": "select_option",
        "description": "Choose an option in a <select> dropdown identified by role+name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "option_label": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["role", "name", "option_label", "reasoning"],
        },
    },
    {
        "name": "navigate",
        "description": "Navigate the browser directly to a URL (only within the allowlisted app).",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "reasoning": {"type": "string"}},
            "required": ["url", "reasoning"],
        },
    },
    {
        "name": "extract",
        "description": (
            "Extract a value from the page for use as an artifact output. The page renders data "
            "in label/value table rows (e.g. a row whose first cell says 'Account Number' and "
            "whose last cell has the value). Give the exact label text of that row."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "row_label": {"type": "string", "description": "Exact label text of the table row to read, e.g. 'Account Number'"},
                "output_name": {"type": "string", "description": "Name to store this value under"},
                "reasoning": {"type": "string"},
            },
            "required": ["row_label", "output_name", "reasoning"],
        },
    },
    {
        "name": "done",
        "description": "Declare the goal accomplished. Provide the final checkpoint you observed that proves it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "checkpoint_text": {"type": "string", "description": "Exact visible text/state proving success"},
                "reasoning": {"type": "string"},
            },
            "required": ["checkpoint_text", "reasoning"],
        },
    },
    {
        "name": "escalate",
        "description": "Give up and request human help because you are stuck / blocked / unsafe to proceed.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]


@dataclass
class Decision:
    tool_name: str
    tool_input: dict
    assistant_text: str = ""


class LLMProvider(abc.ABC):
    @abc.abstractmethod
    def decide(self, messages: list[dict]) -> Decision:
        ...

    @abc.abstractmethod
    def is_real(self) -> bool:
        ...


class AnthropicProvider(LLMProvider):
    """Real, production-shaped provider. Requires ANTHROPIC_API_KEY."""

    def __init__(self, model: str = "claude-sonnet-4-6"):
        import anthropic  # local import so the package doesn't hard-require credentials at import time

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. A genuine discovery run requires real "
                "model API access (see README 'Running a real discovery run')."
            )
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def is_real(self) -> bool:
        return True

    def decide(self, messages: list[dict]) -> Decision:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )
        text_parts = [b.text for b in resp.content if b.type == "text"]
        tool_blocks = [b for b in resp.content if b.type == "tool_use"]
        if not tool_blocks:
            # Model didn't call a tool -- treat as a stall, force escalation upstream.
            return Decision(tool_name="", tool_input={}, assistant_text=" ".join(text_parts))
        tb = tool_blocks[0]
        return Decision(tool_name=tb.name, tool_input=tb.input, assistant_text=" ".join(text_parts))


class ScriptedTestProvider(LLMProvider):
    """
    NOT an LLM. Deterministic canned action sequence for offline scaffold
    testing only. Ignores the observation entirely (aside from logging it).

    DO NOT use this to produce the submission's required discovery-run
    evidence -- only AnthropicProvider counts for that (see README/REPORT).
    """

    def __init__(self, script: list[dict]):
        self._script = list(script)
        self._i = 0

    def is_real(self) -> bool:
        return False

    def decide(self, messages: list[dict]) -> Decision:
        if self._i >= len(self._script):
            return Decision(tool_name="escalate", tool_input={"reason": "scripted actions exhausted"})
        step = self._script[self._i]
        self._i += 1
        return Decision(tool_name=step["tool"], tool_input=step["input"], assistant_text=step.get("note", ""))


def build_provider(name: str, **kwargs) -> LLMProvider:
    if name == "anthropic":
        return AnthropicProvider(**kwargs)
    if name == "scripted-test":
        return ScriptedTestProvider(**kwargs)
    raise ValueError(f"Unknown provider: {name}")
