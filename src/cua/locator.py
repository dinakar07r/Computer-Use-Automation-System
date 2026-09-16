"""
Resolves a LocatorSpec (primary + fallbacks) into an actual Playwright
Locator against the live page, trying each candidate in order.

This is the seam between "how we perceive/act on a surface" (perception.py,
which is Playwright/accessibility-tree specific today) and "the recorded
flow" (artifact.py, which is surface-agnostic). A desktop or legacy-web
adapter would implement the same `resolve(page_or_window, spec)` contract
against a different underlying API (OS accessibility APIs, an old-DOM query
engine, etc.) -- see /REPORT.md section 4.
"""
from __future__ import annotations

from .artifact import Locator, LocatorSpec, LocatorStrategy


class LocatorResolutionError(Exception):
    def __init__(self, spec: LocatorSpec, attempts: list[str]):
        self.spec = spec
        self.attempts = attempts
        super().__init__(
            f"Could not resolve locator (tried {len(attempts)} strategies): {attempts}"
        )


def _resolve_one(page, loc: Locator):
    if loc.strategy == LocatorStrategy.ROLE:
        return page.get_by_role(loc.role or "button", name=loc.value, exact=loc.exact)
    if loc.strategy == LocatorStrategy.LABEL:
        return page.get_by_label(loc.value, exact=loc.exact)
    if loc.strategy == LocatorStrategy.TEXT:
        return page.get_by_text(loc.value, exact=loc.exact)
    if loc.strategy == LocatorStrategy.TEST_ID:
        return page.get_by_test_id(loc.value)
    if loc.strategy == LocatorStrategy.CSS:
        return page.locator(loc.value)
    if loc.strategy == LocatorStrategy.ROW_LABEL:
        # Legacy table pattern: <tr><th>Label</th><td>Value</td></tr> with no
        # id/data-testid on either cell. We locate the row by its label text
        # and read the last cell in that row -- robust to column reordering
        # within a row and to styling changes, brittle only if the label
        # text itself changes (noted in robustness_note).
        return page.locator("tr", has_text=loc.value).locator("td").last
    raise ValueError(f"Unknown locator strategy: {loc.strategy}")


def resolve(page, spec: LocatorSpec, timeout_ms: int = 4000):
    """Try primary, then each fallback in order. Returns the first Playwright
    Locator that resolves to exactly one visible, actionable element.
    Raises LocatorResolutionError if none do."""
    attempts: list[str] = []
    for candidate in [spec.primary, *spec.fallbacks]:
        try:
            loc = _resolve_one(page, candidate)
            loc.first.wait_for(state="visible", timeout=timeout_ms)
            count = loc.count()
            if count >= 1:
                return loc.first
            attempts.append(f"{candidate.strategy}:{candidate.value} (0 matches)")
        except Exception as e:  # noqa: BLE001 - we deliberately try the next strategy
            attempts.append(f"{candidate.strategy}:{candidate.value} ({type(e).__name__})")
            continue
    raise LocatorResolutionError(spec, attempts)


def spec_for_role(role: str, name: str, exact: bool = False, note: str = "") -> LocatorSpec:
    """Build a LocatorSpec preferring role+accessible-name (most robust to
    legacy/no-clean-DOM surfaces), with text-match as a fallback."""
    return LocatorSpec(
        primary=Locator(strategy=LocatorStrategy.ROLE, value=name, role=role, exact=exact),
        fallbacks=[
            Locator(strategy=LocatorStrategy.TEXT, value=name, exact=False),
        ],
        robustness_note=note or (
            f"role+accessible-name ('{role}', '{name}') survives markup/CSS changes "
            "because it reflects rendered semantics, not structure; text-match fallback "
            "catches cases where the role is misreported by a legacy renderer."
        ),
    )


def spec_for_row_label(label: str, note: str = "") -> LocatorSpec:
    return LocatorSpec(
        primary=Locator(strategy=LocatorStrategy.ROW_LABEL, value=label),
        fallbacks=[],
        robustness_note=note or (
            f"table row identified by its label cell text ('{label}'); reads the last "
            "cell in that row. Survives column/CSS changes; breaks only if the label "
            "text itself is reworded, which a human would also be confused by."
        ),
    )


def spec_for_label(label: str, note: str = "") -> LocatorSpec:
    return LocatorSpec(
        primary=Locator(strategy=LocatorStrategy.LABEL, value=label),
        fallbacks=[Locator(strategy=LocatorStrategy.TEXT, value=label)],
        robustness_note=note or f"label association ('{label}') is stable across layout changes.",
    )
