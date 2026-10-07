"""Campaign plans: one design document, several sequential campaigns.

A design document may declare a campaign split in a fenced block:

    ```sliceme-campaigns
    [
      {"name": "core", "target": "feat/core", "base": "main",
       "dirs": ["src", "include"]},
      {"name": "api", "target": "feat/api", "base": "feat/core",
       "dirs": ["python", "bindings"]}
    ]
    ```

Each entry becomes one campaign.  The coordinator runs the entries in order.
One campaign owns only the directories in its ``dirs`` scope, so two campaigns
may reuse a directory.  That is the point of the split: the engine keeps the
one-writer-per-directory rule inside one campaign, and the campaign boundary
lets the next campaign own the same directory.

This module parses and validates the plan.  The coordinator owns the planner,
the target branches, and the run order.  The engine reads the plan for
``sliceme plan`` and for the campaign registry join.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .util import SlicemeError

#: The fenced-block info string that introduces a campaign plan.
PLAN_FENCE = "sliceme-campaigns"

_FENCE = re.compile(
    r"^[ \t]*```+[^\n]*\b" + re.escape(PLAN_FENCE) + r"\b[^\n]*\r?\n(.*?)^[ \t]*```+[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


def parse_campaign_plan(text: str) -> list[dict[str, Any]]:
    """Parse the ``sliceme-campaigns`` fenced block.

    Returns an empty list when the text has no plan.  Raises
    :class:`SlicemeError` when the block exists but is not valid.
    """
    match = _FENCE.search(text or "")
    if match is None:
        return []
    raw = match.group(1).strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SlicemeError(f"campaign plan is not valid JSON: {error}") from error
    if not isinstance(data, list):
        raise SlicemeError("campaign plan must be a JSON list")
    return _validate(data)


def _validate(entries: list[Any]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SlicemeError(f"campaign plan entry {index} must be an object")
        name = str(entry.get("name") or "").strip()
        target = str(entry.get("target") or "").strip()
        if not name:
            raise SlicemeError(f"campaign plan entry {index} needs a name")
        if not target:
            raise SlicemeError(f"campaign plan entry '{name}' needs a target")
        if name in seen:
            raise SlicemeError(f"campaign plan name '{name}' appears twice")
        seen.add(name)
        dirs = entry.get("dirs") or []
        if not isinstance(dirs, list) or any(not isinstance(d, str) for d in dirs):
            raise SlicemeError(
                f"campaign plan entry '{name}' dirs must be a list of strings"
            )
        base = entry.get("base")
        plan.append(
            {
                "name": name,
                "target": target,
                "base": str(base).strip() if base else None,
                "dirs": [str(d).strip() for d in dirs if str(d).strip()],
            }
        )
    return plan


def load_campaign_plan(root: Path | str, design: str | Path) -> list[dict[str, Any]]:
    """Read *design* (relative to *root*) and parse its campaign plan."""
    path = Path(design)
    if not path.is_absolute():
        path = Path(root) / path
    if not path.is_file():
        raise SlicemeError(f"design document not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SlicemeError(f"cannot read design document {path}: {error}") from error
    return parse_campaign_plan(text)


__all__ = [
    "PLAN_FENCE",
    "load_campaign_plan",
    "parse_campaign_plan",
]
