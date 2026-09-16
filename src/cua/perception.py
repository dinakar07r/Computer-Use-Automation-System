"""
Perception: turns a live Playwright page into a compact textual observation
the LLM can reason over, using the accessibility tree rather than raw DOM/HTML
or a screenshot.

Why the accessibility tree and not raw DOM or a screenshot:
  - It's what the brief calls out as "often more stable than raw markup, and
    available on desktop apps too" -- the same abstraction extends to OS-level
    accessibility APIs for a native desktop surface (see /REPORT.md section 4).
  - It survives legacy markup (frameset/table layouts, no data-testid, no
    stable ids) because it's built from rendered semantics (role + accessible
    name), which is exactly what a human operator perceives too.
  - It's far more token-efficient than raw HTML or a screenshot, which matters
    for keeping the observe->decide->act loop fast and cheap.

Screenshot-based / coordinate control remains available as a fallback
mechanism (see discovery_agent.py) for elements the accessibility tree can't
resolve -- but the primary channel is this one.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AXNode:
    role: str
    name: str
    children: list["AXNode"] = field(default_factory=list)
    value: str | None = None


def _prune(node: dict, depth: int = 0, max_depth: int = 40) -> dict | None:
    if node is None or depth > max_depth:
        return None
    role = node.get("role", "")
    if role in ("none", "presentation", "generic", "InlineTextBox"):
        # collapse into children to keep the tree compact
        pass
    children = []
    for c in node.get("children", []) or []:
        pc = _prune(c, depth + 1, max_depth)
        if pc:
            children.append(pc)
    pruned = {
        "role": role,
        "name": node.get("name", "") or "",
    }
    if node.get("value"):
        pruned["value"] = node.get("value")
    if children:
        pruned["children"] = children
    # Drop pure-noise nodes with no name/value/children
    if not pruned["name"] and not pruned.get("value") and not children and role not in (
        "textbox", "button", "link", "combobox", "checkbox", "radio",
    ):
        return None
    return pruned


def snapshot(page) -> dict:
    """Return a pruned accessibility tree for the current page."""
    tree = page.accessibility.snapshot(interesting_only=True)
    pruned = _prune(tree or {})
    return pruned or {"role": "WebArea", "name": ""}


def render_for_llm(page, max_chars: int = 6000) -> str:
    """Render the current page as a compact, LLM-readable observation."""
    tree = snapshot(page)
    lines: list[str] = []

    def walk(node, indent=0):
        if not node:
            return
        role = node.get("role", "")
        name = node.get("name", "")
        val = node.get("value", "")
        label = f"{'  ' * indent}- [{role}]"
        if name:
            label += f' "{name}"'
        if val:
            label += f" (value={val!r})"
        lines.append(label)
        for c in node.get("children", []) or []:
            walk(c, indent + 1)

    walk(tree)
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [truncated]"
    header = f"URL: {page.url}\nTITLE: {page.title()}\n\nACCESSIBILITY TREE:\n"
    return header + text
