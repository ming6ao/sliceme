"""Per-node child progress channel (``.sliceme/<key>.progress/``).

The pi package extension writes two engine-owned file kinds per node in a
campaign; the engine reads them and writes nothing:

``.sliceme/<branch-key>.progress/<node>.json``
    the last event of one child, as one JSON object (the mirror);
``.sliceme/<branch-key>.progress/<node>.jsonl``
    the append-only event tail, rotated by the writer at a byte cap.

One event is one JSON line::

    {"node": "n7", "step": 4, "name": "edit payments.py", "tool": "edit",
     "ts": 1731234567.5, "detail": "", "run_id": "node-n7"}

The writer keeps ``run_id`` (the workflow child key), so ``record`'s
per-node try count is the number of distinct ``run_id`` values in the tail:
a first run writes ``node-n7``, a salvage retake writes
``node-n7-try-2``.  A child that never wrote progress (the slice tool
fallback is optional) counts as one try.

The engine never rotates and never prunes the channel: the writer owns both
files, the engine only reads.  Path helpers live in :mod:`sliceme.campaign`
beside the other per-campaign state paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .campaign import progress_event_path, progress_tail_path

#: A node with no event for this many seconds counts as stalled.
STALL_AFTER_SECONDS = 120.0

#: The writer's tail rotation cap. The engine copies this share of the tail
#: for evidence and keeps its own reads bounded; the writer owns the file.
PROGRESS_TAIL_BYTES = 65536


#: The salvage retake's run-key suffix; the campaign resource builds
#: ``node-<id>-try-2``, so a retained tail key ending in this suffix is a
#: retake's run.
SALVAGE_RUN_SUFFIX = "-try-2"


def _coerce_event(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    node = value.get("node")
    if not isinstance(node, str) or not node:
        return None
    return value


def read_tail(path: Path, *, max_bytes: int = PROGRESS_TAIL_BYTES) -> list[dict[str, Any]]:
    """Parse the event lines of one tail, oldest first, bounded to the end.

    A missing or unreadable tail means no events: a child that never wrote
    progress reports an empty history, never an error.
    """
    try:
        raw = path.read_text(encoding="utf8", errors="replace")
    except OSError:
        return []
    if len(raw.encode("utf8")) > max_bytes and raw:
        # The writer rotated by size; keep the last share of the bytes.
        raw = raw[-max_bytes:]
        # Drop a partial first line.
        first = raw.find("\n")
        if first >= 0:
            raw = raw[first + 1 :]
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if _coerce_event(event) is not None:
            events.append(event)
    return events


def run_ids(events: list[dict[str, Any]]) -> list[str]:
    """The distinct ``run_id`` values of one event list, first seen order."""
    seen: list[str] = []
    for event in events:
        run = str(event.get("run_id") or "").strip()
        if run and run not in seen:
            seen.append(run)
    return seen


def try_count(events: list[dict[str, Any]]) -> int:
    """One node's try count: the tail's distinct ``run_id`` values.

    The writer keeps only the newest slice of an appended tail, so a busy
    first run can rotate its own ``run_id`` out while the salvage retake's
    ``-try-2`` key survives: a retained retake key proves at least two tries
    even with one distinct ``run_id`` left.  A node with no events is one
    try, never zero.
    """
    ids = run_ids(events)
    salvaged = any(value.endswith(SALVAGE_RUN_SUFFIX) for value in ids)
    return max(2 if salvaged else 1, len(ids))


def node_row(node: str, events: list[dict[str, Any]], now: float) -> dict[str, Any]:
    """One reader row for *node* from its parsed events.

    ``stalled_ms`` is the age of the last event; ``None`` when the node
    never wrote an event.  ``attention`` is the engine's stall verdict: a
    last event at least :data:`STALL_AFTER_SECONDS` old.
    """
    last = events[-1] if events else None
    step = last.get("step") if last else None
    last_ts = last.get("ts") if last else None
    try:
        last_ts_value = float(last_ts) if last_ts is not None else None
    except (TypeError, ValueError):
        last_ts_value = None
    stalled_ms: int | None = None
    if last_ts_value is not None:
        stalled_ms = max(0, int((now - last_ts_value) * 1000))
    return {
        "node": node,
        "step": int(step) if isinstance(step, (int, float)) else None,
        "last_ts": last_ts_value,
        "stalled_ms": stalled_ms,
        "attention": stalled_ms is not None and stalled_ms >= STALL_AFTER_SECONDS * 1000,
    }


def rows(
    root: Path,
    branch: str,
    nodes: list[str] | None = None,
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Reader rows for the progress channel of one campaign.

    With no ``nodes``, the channel reports every node that wrote an event,
    ordered by node id.  With *nodes*, the rows follow that order.
    """
    from .util import now as util_now

    if now is None:
        now = util_now()
    directory = progress_tail_path(root, branch, "").parent
    if nodes is not None:
        watched = [str(node) for node in nodes]
    else:
        watched = sorted(
            path.stem
            for path in directory.glob("*.json")
            if path.is_file()
        )
        for path in directory.glob("*.jsonl"):
            if path.is_file():
                node = path.stem
                if node not in watched:
                    watched.append(node)
        watched.sort()
    rows: list[dict[str, Any]] = []
    for node in watched:
        events = read_tail(progress_tail_path(root, branch, node))
        rows.append(node_row(node, events, now))
    return rows


def index(root: Path, branch: str, node: str) -> dict[str, Any]:
    """The mirror object plus the parsed tail events of one node."""
    return {
        "tail": read_tail(progress_tail_path(root, branch, node)),
        "mirror": _read_mirror(progress_event_path(root, branch, node)),
    }


def _read_mirror(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf8"))
    except (OSError, ValueError):
        return None


__all__ = [
    "PROGRESS_TAIL_BYTES",
    "SALVAGE_RUN_SUFFIX",
    "STALL_AFTER_SECONDS",
    "index",
    "node_row",
    "read_tail",
    "rows",
    "run_ids",
    "try_count",
]
