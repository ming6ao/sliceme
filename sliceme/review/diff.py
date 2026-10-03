"""Parse git diffs for the review page.

The client renders one file at a time.  The server sends a file index for the
whole packet and a line list for one file, so the first load stays small.

Every function here is read-only.  The git calls use the three-dot form
``target...source`` so the diff is against the merge base, never a moving tip.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import gitutil

__all__ = ["file_diff", "file_index", "parse_unified_diff", "commits"]

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _three_dot(target: str, source: str) -> str:
    return f"{target}...{source}"


def commits(root: Path, target: str, source: str) -> list[dict[str, Any]]:
    """Newest-last commits reachable from *source* but not from *target*.

    Uses the two-dot range on purpose: the review unit is the commits the
    source tip adds on top of the merge base, and the client lists them so a
    reviewer can select one.
    """
    fmt = "%H%x1f%h%x1f%s%x1f%an%x1f%aI"
    res = gitutil.git(
        root, "log", "--reverse", f"--format={fmt}", f"{target}..{source}", check=False
    )
    if not res.ok:
        return []
    rows: list[dict[str, Any]] = []
    for line in res.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 5:
            continue
        rows.append(
            {
                "hash": parts[0],
                "short": parts[1],
                "subject": parts[2],
                "author": parts[3],
                "date": parts[4],
            }
        )
    return rows


def _name_status(root: Path, target: str, source: str) -> list[tuple[str, str, str | None]]:
    res = gitutil.git(
        root, "diff", "--name-status", "-M", _three_dot(target, source), check=False
    )
    if not res.ok:
        return []
    entries: list[tuple[str, str, str | None]] = []
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status[:1] in {"R", "C"} and len(parts) >= 3:
            entries.append((status, parts[2], parts[1]))
        else:
            entries.append((status, parts[1], None))
    return entries


def _numstat(root: Path, target: str, source: str, path: str) -> tuple[int, int, bool]:
    res = gitutil.git(
        root,
        "diff",
        "--numstat",
        "-M",
        _three_dot(target, source),
        "--",
        path,
        check=False,
    )
    if not res.ok or not res.stdout.strip():
        return 0, 0, False
    first = res.stdout.splitlines()[0]
    parts = first.split("\t")
    if len(parts) < 3:
        return 0, 0, False
    if parts[0] == "-" or parts[1] == "-":
        return 0, 0, True
    try:
        return int(parts[0]), int(parts[1]), False
    except ValueError:
        return 0, 0, False


def file_index(root: Path, target: str, source: str) -> list[dict[str, Any]]:
    """The changed-file rows for the packet: path, status, additions, deletions."""
    rows: list[dict[str, Any]] = []
    for status, path, old in _name_status(root, target, source):
        additions, deletions, binary = _numstat(root, target, source, path)
        rows.append(
            {
                "path": path,
                "old_path": old,
                "status": status[:1],
                "additions": additions,
                "deletions": deletions,
                "binary": binary,
            }
        )
    rows.sort(key=lambda row: row["path"])
    return rows


def parse_unified_diff(text: str) -> list[dict[str, Any]]:
    """Turn one file's unified diff into renderable lines.

    Each row holds a ``type`` (``hunk``, ``context``, ``add``, or ``delete``),
    the old and new line numbers, and the raw text.  Header lines are dropped.
    """
    lines: list[dict[str, Any]] = []
    old_no = 0
    new_no = 0
    for raw in text.splitlines():
        match = _HUNK_RE.match(raw)
        if match:
            old_no = int(match.group(1))
            new_no = int(match.group(3))
            lines.append(
                {"type": "hunk", "old": None, "new": None, "text": raw}
            )
            continue
        if raw.startswith(("diff --git ", "index ", "--- ", "+++ ", "new file", "deleted file", "similarity", "rename ", "old mode", "new mode", "Binary files ")):
            continue
        if raw.startswith("\\"):
            lines.append({"type": "meta", "old": None, "new": None, "text": raw})
            continue
        if raw.startswith("+"):
            lines.append({"type": "add", "old": None, "new": new_no, "text": raw[1:]})
            new_no += 1
            continue
        if raw.startswith("-"):
            lines.append({"type": "delete", "old": old_no, "new": None, "text": raw[1:]})
            old_no += 1
            continue
        if raw.startswith(" "):
            lines.append(
                {"type": "context", "old": old_no, "new": new_no, "text": raw[1:]}
            )
            old_no += 1
            new_no += 1
            continue
        # A trailing empty line or an unexpected header: keep it as context.
        if raw:
            lines.append({"type": "context", "old": old_no, "new": new_no, "text": raw})
            old_no += 1
            new_no += 1
    return lines


def file_diff(root: Path, target: str, source: str, path: str) -> list[dict[str, Any]]:
    """The parsed line list for one file in the packet diff."""
    res = gitutil.git(
        root,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "-M",
        _three_dot(target, source),
        "--",
        path,
        check=False,
    )
    if not res.ok:
        return []
    return parse_unified_diff(res.stdout)
