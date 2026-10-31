"""Per-step timing statistics for a campaign.

Pi-subagents writes one transcript for each child run under
``<session-dir>/subagent-artifacts/<run>_<agent>_transcript.jsonl``.  A
transcript record is either an assistant message with its model-call duration,
or a tool start and end pair with the tool arguments.  This module turns those
records into one ``timings`` row per thinking step and per tool call.

The engine ingests the transcripts at ``record``, ``check``, and ``report``, so
the rows survive the pi-subagents artifact cleanup.  The ingest is
best-effort: a missing or partial transcript never fails a campaign verb.

The campaign scope comes from two signals:

- a worker or a reviewer runs in the campaign worktree;
- the planner runs in the repository root, and its run time falls inside the
  campaign window (from ``start`` to the first record).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .util import branch_key, now, read_json, state_dir

#: The default slow-command threshold, in seconds.
SLOW_SECONDS = 120.0

#: The longest command text the store keeps for one tool call.
COMMAND_LIMIT = 2000

#: The shortest command text the report shows for a call below the threshold.
PREVIEW_LIMIT = 160

#: The number of command rows a report shows when no call is slow.
TOP_COMMANDS = 10

#: The hard cap on command rows in one report.
MAX_COMMAND_ROWS = 25

#: The child agents that the Sliceme workflow resources launch.
CAMPAIGN_AGENTS = frozenset({"sliceme-planner", "sliceme-worker", "reviewer"})


def pi_session_path(root: Path, branch: str) -> Path:
    """The adapter-written pi session binding for a campaign."""
    return state_dir(root) / f"{branch_key(branch)}.pi-session.json"


def transcript_dir(session_file: str | os.PathLike[str]) -> Path:
    """The pi-subagents artifact directory beside a pi session file."""
    return Path(session_file).parent / "subagent-artifacts"


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------
def _seconds(value: Any) -> float | None:
    """One epoch value as seconds; accept milliseconds and ISO text too."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        # An epoch in milliseconds is far above a plausible second value.
        return number / 1000.0 if number > 1e11 else number
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _tool_command(record: dict[str, Any]) -> tuple[str | None, bool]:
    """The command text of one tool call, and whether the store cut it."""
    raw = record.get("argsPayload")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except ValueError:
            return None, False
    elif isinstance(raw, dict):
        payload = raw
    else:
        return None, False
    if not isinstance(payload, dict):
        return None, False
    command = payload.get("command")
    if not isinstance(command, str) or not command.strip():
        return None, False
    command = command.strip()
    if len(command) <= COMMAND_LIMIT:
        return command, False
    return command[:COMMAND_LIMIT], True


def _bound_preview(command: str | None) -> str | None:
    if not command:
        return None
    text = " ".join(command.split())
    return text if len(text) <= PREVIEW_LIMIT else text[:PREVIEW_LIMIT] + "..."


def parse_transcript(path: Path) -> dict[str, Any]:
    """One child transcript as its run identity and its timing steps.

    A thinking step is one assistant message.  Its duration is the model-call
    duration that pi recorded for that message.  A tool step is one
    ``tool_start``/``tool_end`` pair.  An unpaired or malformed record is
    skipped, because a killed child can leave a partial transcript.
    """
    run = None
    agent = None
    cwd = None
    child_index = 0
    starts: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []
    thinking_index = 0
    for record in _read_jsonl(path):
        if run is None:
            run = record.get("runId")
            agent = record.get("agent")
            cwd = record.get("cwd")
            child_index = int(record.get("childIndex") or 0)
        kind = record.get("recordType")
        stamp = _seconds(record.get("ts"))
        if kind == "message" and record.get("role") == "assistant":
            message = record.get("message") or {}
            duration_ms = message.get("durationMs")
            if not isinstance(duration_ms, (int, float)) or duration_ms < 0:
                continue
            if stamp is None:
                continue
            thinking_index += 1
            duration = float(duration_ms) / 1000.0
            steps.append(
                {
                    "step_id": f"{run or path.name}:thinking:{thinking_index}",
                    "kind": "thinking",
                    "tool": None,
                    "command": None,
                    "command_truncated": False,
                    "is_error": False,
                    "duration": duration,
                    "started_at": max(0.0, stamp - duration),
                    "ended_at": stamp,
                }
            )
        elif kind == "tool_start":
            call_id = record.get("toolCallId")
            if isinstance(call_id, str) and call_id:
                starts[call_id] = record
        elif kind == "tool_end":
            call_id = record.get("toolCallId")
            if not isinstance(call_id, str) or call_id not in starts:
                continue
            start = starts.pop(call_id)
            begin = _seconds(start.get("ts"))
            if begin is None or stamp is None or stamp < begin:
                continue
            command, truncated = _tool_command(start)
            steps.append(
                {
                    "step_id": f"{run or path.name}:tool:{call_id}",
                    "kind": "tool",
                    "tool": str(start.get("toolName") or "tool"),
                    "command": command,
                    "command_truncated": truncated,
                    "is_error": bool(record.get("isError")),
                    "duration": stamp - begin,
                    "started_at": begin,
                    "ended_at": stamp,
                }
            )
    return {
        "run": run,
        "agent": agent,
        "cwd": cwd,
        "child_index": child_index,
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# Campaign mapping and ingest
# ---------------------------------------------------------------------------
def _real(path: Any) -> str | None:
    if not isinstance(path, (str, os.PathLike)):
        return None
    text = os.fspath(path)
    if not text:
        return None
    try:
        return os.path.realpath(text)
    except OSError:
        return None


def _inside(path: Any, parent: Any) -> bool:
    child = _real(path)
    root = _real(parent)
    if not child or not root:
        return False
    return child == root or child.startswith(root + os.sep)


def _same(path: Any, parent: Any) -> bool:
    child = _real(path)
    root = _real(parent)
    return bool(child and root and child == root)


def _belongs(
    *,
    cwd: Any,
    agent: str | None,
    meta: dict[str, Any],
    worktree: Any,
    repo_root: Path | None,
    created_at: float,
    first_record_at: float | None,
) -> bool:
    """Whether one child run belongs to the campaign."""
    if _inside(cwd, worktree):
        return True
    if agent != "sliceme-planner" or repo_root is None:
        return False
    if not _same(cwd, repo_root):
        return False
    stamp = _seconds(meta.get("timestamp"))
    if stamp is None:
        return False
    low = created_at - 60.0 if created_at > 0 else 0.0
    high = (first_record_at or now()) + 300.0
    return low <= stamp <= high


def _session_binding(root: Path, branch: str) -> dict[str, Any]:
    """The pi session file and id for one campaign.

    The adapter writes the binding on each engine tool call.  An older plane
    can fall back to the suspend/resume descriptor, which also names the pi
    session.
    """
    binding = read_json(pi_session_path(root, branch))
    if isinstance(binding, dict):
        pi = binding.get("pi")
        if isinstance(pi, dict) and pi.get("session_file"):
            return pi
    descriptor = read_json(state_dir(root) / f"{branch_key(branch)}.session.json")
    if isinstance(descriptor, dict):
        pi = descriptor.get("pi")
        if isinstance(pi, dict):
            return pi
    return {}


def ingest(
    root: Path,
    store: Any,
    *,
    campaign: str | None,
    branch: str,
) -> dict[str, Any]:
    """Store the timing steps of the campaign child runs.

    The function returns counts and never raises for a missing session, a
    missing artifact directory, or a partial transcript.
    """
    result: dict[str, Any] = {
        "runs": 0,
        "thinking": 0,
        "tools": 0,
        "inserted": 0,
    }
    if not campaign:
        return result
    row = store.get_campaign(campaign)
    if not row:
        return result
    binding = _session_binding(root, branch)
    session_file = binding.get("session_file")
    if not isinstance(session_file, str) or not session_file:
        return result
    artifacts = transcript_dir(session_file)
    if not artifacts.is_dir():
        return result
    worktree = row.get("worktree")
    created_at = float(row.get("created_at") or 0.0)
    records = store.list_records(campaign=str(row.get("key") or campaign))
    first_record_at = min(
        (float(r.get("created_at") or 0.0) for r in records if r.get("created_at")),
        default=None,
    )
    seen: set[str] = set()
    pending: list[dict[str, Any]] = []
    try:
        known = store.timing_runs(campaign=str(row.get("key") or campaign))
    except Exception:
        known = set()
    try:
        metas = sorted(artifacts.glob("*_meta.json"))
    except OSError:
        return result
    for meta_path in metas:
        try:
            meta = read_json(meta_path, {})
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        agent = str(meta.get("agent") or "") or None
        if agent not in CAMPAIGN_AGENTS:
            continue
        # A run with timing rows is complete: its transcript was read at an
        # earlier phase boundary, so a re-read cannot add a step.
        declared_run = str(meta.get("runId") or "")
        if declared_run and declared_run in known:
            continue
        transcript = meta.get("transcriptPath")
        path = Path(transcript) if isinstance(transcript, str) and transcript else None
        if path is None:
            stem = meta_path.name[: -len("_meta.json")]
            path = artifacts / f"{stem}_transcript.jsonl"
        if not path.is_file():
            continue
        parsed = parse_transcript(path)
        if not parsed["steps"]:
            continue
        if not _belongs(
            cwd=parsed.get("cwd"),
            agent=agent,
            meta=meta,
            worktree=worktree,
            repo_root=root,
            created_at=created_at,
            first_record_at=first_record_at,
        ):
            continue
        run = str(parsed.get("run") or meta.get("runId") or path.stem)
        if run in seen or run in known:
            continue
        seen.add(run)
        stamp = now()
        rows = [
            {
                **step,
                "campaign": str(row.get("key") or campaign),
                "run": run,
                "agent": agent,
                "child_index": int(parsed.get("child_index") or 0),
                "source": str(path),
                "created_at": stamp,
            }
            for step in parsed["steps"]
        ]
        result["runs"] += 1
        result["thinking"] += sum(1 for item in rows if item["kind"] == "thinking")
        result["tools"] += sum(1 for item in rows if item["kind"] == "tool")
        pending.extend(rows)
    if pending:
        try:
            result["inserted"] = store.add_timings(pending)
        except Exception as exc:  # the timing record is never a gate
            result["error"] = str(exc)
    return result


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def _percentile(ordered: Sequence[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return float(ordered[low]) * (1.0 - weight) + float(ordered[high]) * weight


def _group(durations: Sequence[float]) -> dict[str, Any]:
    ordered = sorted(float(value) for value in durations)
    total = sum(ordered)
    count = len(ordered)
    return {
        "count": count,
        "total": round(total, 3),
        "mean": round(total / count, 3) if count else 0.0,
        "p50": round(_percentile(ordered, 0.50), 3),
        "p90": round(_percentile(ordered, 0.90), 3),
        "max": round(ordered[-1], 3) if ordered else 0.0,
    }


def _command_row(row: dict[str, Any], slow_seconds: float) -> dict[str, Any]:
    duration = float(row.get("duration") or 0.0)
    return {
        "duration": round(duration, 3),
        "slow": duration >= slow_seconds,
        "tool": row.get("tool"),
        "agent": row.get("agent"),
        "run": row.get("run"),
        "is_error": bool(row.get("is_error")),
        "truncated": bool(row.get("command_truncated")),
        "command": row.get("command"),
    }


def summarize(
    rows: Sequence[dict[str, Any]], *, slow_seconds: float = SLOW_SECONDS
) -> dict[str, Any]:
    """Group the timing rows by step kind, by agent, by tool, and by command."""
    threshold = float(slow_seconds) if slow_seconds and slow_seconds > 0 else SLOW_SECONDS
    thinking = [row for row in rows if row.get("kind") == "thinking"]
    tools = [row for row in rows if row.get("kind") == "tool"]

    by_agent: dict[str, dict[str, Any]] = {}
    for agent in sorted({str(row.get("agent") or "unknown") for row in rows}):
        picked = [row for row in rows if str(row.get("agent") or "unknown") == agent]
        by_agent[agent] = {
            "thinking": _group(
                [float(r.get("duration") or 0.0) for r in picked if r.get("kind") == "thinking"]
            ),
            "tools": _group(
                [float(r.get("duration") or 0.0) for r in picked if r.get("kind") == "tool"]
            ),
        }

    by_tool: dict[str, dict[str, Any]] = {}
    for tool in sorted({str(row.get("tool") or "tool") for row in tools}):
        picked = [row for row in tools if str(row.get("tool") or "tool") == tool]
        group = _group([float(r.get("duration") or 0.0) for r in picked])
        group["errors"] = sum(1 for r in picked if r.get("is_error"))
        by_tool[tool] = group

    runs: dict[str, dict[str, Any]] = {}
    for run in sorted({str(row.get("run") or "run") for row in rows}):
        picked = [row for row in rows if str(row.get("run") or "run") == run]
        agent = str(picked[0].get("agent") or "unknown") if picked else "unknown"
        starts = [float(r["started_at"]) for r in picked if r.get("started_at")]
        ends = [float(r["ended_at"]) for r in picked if r.get("ended_at")]
        runs[run] = {
            "agent": agent,
            "thinking": _group(
                [float(r.get("duration") or 0.0) for r in picked if r.get("kind") == "thinking"]
            ),
            "tools": _group(
                [float(r.get("duration") or 0.0) for r in picked if r.get("kind") == "tool"]
            ),
            "started_at": min(starts) if starts else None,
            "ended_at": max(ends) if ends else None,
        }

    command_rows = [_command_row(row, threshold) for row in tools if row.get("command")]
    command_rows.sort(key=lambda item: item["duration"], reverse=True)
    selected = [row for row in command_rows if row["slow"]]
    if len(selected) < TOP_COMMANDS:
        for row in command_rows:
            if row not in selected:
                selected.append(row)
            if len(selected) >= TOP_COMMANDS:
                break
    selected.sort(key=lambda item: item["duration"], reverse=True)

    starts = [float(r["started_at"]) for r in rows if r.get("started_at")]
    ends = [float(r["ended_at"]) for r in rows if r.get("ended_at")]
    return {
        "steps": len(rows),
        "slow_seconds": threshold,
        "wall": round(max(ends) - min(starts), 3) if starts and ends else 0.0,
        "thinking": _group([float(r.get("duration") or 0.0) for r in thinking]),
        "tools": _group([float(r.get("duration") or 0.0) for r in tools]),
        "by_agent": by_agent,
        "by_tool": by_tool,
        "runs": runs,
        "commands": command_rows[:MAX_COMMAND_ROWS],
        "longest_commands": selected[:MAX_COMMAND_ROWS],
        "slow_commands": [row for row in command_rows if row["slow"]][:MAX_COMMAND_ROWS],
    }


def format_duration(seconds: Any) -> str:
    """One duration as a short readable text."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "?s"
    if value < 60:
        return f"{value:.1f}s"
    if value < 3600:
        return f"{value / 60:.1f}m"
    return f"{value / 3600:.2f}h"


def _days(stamp: Any) -> str:
    try:
        return datetime.fromtimestamp(float(stamp), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except (TypeError, ValueError, OSError):
        return "-"


def _cell(text: Any) -> str:
    """One Markdown table cell: collapse whitespace and protect the pipes."""
    value = " ".join(str(text or "").split())
    return value.replace("|", "\\|").replace("`", "'")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render(
    stats: dict[str, Any],
    *,
    campaign: str,
    branch: str,
    worktree: str | None = None,
    slow_seconds: float | None = None,
) -> str:
    """The dedicated timing report as Markdown."""
    threshold = float(
        slow_seconds if slow_seconds is not None else stats.get("slow_seconds") or SLOW_SECONDS
    )
    lines = [f"# Timing report: {campaign}", ""]
    lines.append(f"- Campaign branch: `{branch}`")
    if worktree:
        lines.append(f"- Worktree: `{worktree}`")
    lines.append(f"- Steps: {stats.get('steps', 0)}")
    lines.append(f"- Active span: {format_duration(stats.get('wall'))}")
    lines.append(f"- Slow-command threshold: {format_duration(threshold)}")
    lines.append("")
    lines.append(
        "The totals sum every child run. Parallel workers can overlap, so a "
        "total can be larger than the active span."
    )
    lines.append("")

    lines.extend(["## Summary", ""])
    lines.append("| group | count | total | mean | p50 | p90 | max |")
    lines.append("|---|---|---|---|---|---|---|")
    for label, group in (
        ("thinking", stats.get("thinking") or {}),
        ("tool call", stats.get("tools") or {}),
    ):
        lines.append(
            "| {label} | {count} | {total} | {mean} | {p50} | {p90} | {max} |".format(
                label=label,
                count=group.get("count", 0),
                total=format_duration(group.get("total")),
                mean=format_duration(group.get("mean")),
                p50=format_duration(group.get("p50")),
                p90=format_duration(group.get("p90")),
                max=format_duration(group.get("max")),
            )
        )
    lines.append("")

    lines.extend(["## Thinking steps", ""])
    by_agent = stats.get("by_agent") or {}
    if not by_agent:
        lines.extend(["(no thinking step was recorded)", ""])
    else:
        lines.append("| agent | count | total | mean | p50 | p90 | max |")
        lines.append("|---|---|---|---|---|---|---|")
        for agent, groups in by_agent.items():
            group = groups.get("thinking") or {}
            lines.append(
                "| {agent} | {count} | {total} | {mean} | {p50} | {p90} | {max} |".format(
                    agent=agent,
                    count=group.get("count", 0),
                    total=format_duration(group.get("total")),
                    mean=format_duration(group.get("mean")),
                    p50=format_duration(group.get("p50")),
                    p90=format_duration(group.get("p90")),
                    max=format_duration(group.get("max")),
                )
            )
        lines.append("")

    lines.extend(["## Tool calls", ""])
    by_tool = stats.get("by_tool") or {}
    if not by_tool:
        lines.extend(["(no tool call was recorded)", ""])
    else:
        lines.append("| tool | count | total | mean | p90 | max | errors |")
        lines.append("|---|---|---|---|---|---|---|")
        for tool, group in by_tool.items():
            lines.append(
                "| {tool} | {count} | {total} | {mean} | {p90} | {max} | {errors} |".format(
                    tool=tool,
                    count=group.get("count", 0),
                    total=format_duration(group.get("total")),
                    mean=format_duration(group.get("mean")),
                    p90=format_duration(group.get("p90")),
                    max=format_duration(group.get("max")),
                    errors=group.get("errors", 0),
                )
            )
        lines.append("")

    lines.extend(["## Longest commands", ""])
    commands = stats.get("longest_commands") or []
    if not commands:
        lines.extend(["(no tool call carried a command)", ""])
    else:
        lines.append("| duration | tool | agent | status | command |")
        lines.append("|---|---|---|---|---|")
        for row in commands:
            command = str(row.get("command") or "")
            if not row.get("slow"):
                command = _bound_preview(command) or ""
            if row.get("truncated"):
                command += " [command cut]"
            command = _cell(command)
            lines.append(
                "| {duration} | {tool} | {agent} | {status} | `{command}` |".format(
                    duration=format_duration(row.get("duration")),
                    tool=_cell(row.get("tool") or "-"),
                    agent=_cell(row.get("agent") or "-"),
                    status="error" if row.get("is_error") else "ok",
                    command=command,
                )
            )
        lines.append("")
        slow = stats.get("slow_commands") or []
        if slow:
            lines.append(
                f"{len(slow)} command(s) ran for at least {format_duration(threshold)}. "
                "A slow command is shown in full, and a faster command shows a "
                "short preview."
            )
        else:
            lines.append(
                f"No command ran for at least {format_duration(threshold)}. "
                "The table shows a short command preview."
            )
        lines.append("")

    lines.extend(["## Child runs", ""])
    runs = stats.get("runs") or {}
    if not runs:
        lines.extend(["(no child run was recorded)", ""])
    else:
        lines.append("| run | agent | started | span | thinking | tools |")
        lines.append("|---|---|---|---|---|---|")
        for run, entry in runs.items():
            thinking = entry.get("thinking") or {}
            tools = entry.get("tools") or {}
            started = entry.get("started_at")
            ended = entry.get("ended_at")
            span = (
                format_duration(float(ended) - float(started))
                if started is not None and ended is not None
                else "-"
            )
            lines.append(
                "| `{run}` | {agent} | {started} | {span} | {thinking} | {tools} |".format(
                    run=str(run)[:12],
                    agent=entry.get("agent") or "-",
                    started=_days(started) if started is not None else "-",
                    span=span,
                    thinking=format_duration(thinking.get("total")),
                    tools=format_duration(tools.get("total")),
                )
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_section(stats: dict[str, Any], *, path: str) -> list[str]:
    """The short timing block of the main campaign report."""
    threshold = stats.get("slow_seconds") or SLOW_SECONDS
    thinking = stats.get("thinking") or {}
    tools = stats.get("tools") or {}
    slow = stats.get("slow_commands") or []
    lines = ["## Timing", ""]
    lines.append(
        "- Thinking steps: {count} (total {total}, mean {mean}, p90 {p90}, max {max})".format(
            count=thinking.get("count", 0),
            total=format_duration(thinking.get("total")),
            mean=format_duration(thinking.get("mean")),
            p90=format_duration(thinking.get("p90")),
            max=format_duration(thinking.get("max")),
        )
    )
    lines.append(
        "- Tool calls: {count} (total {total}, mean {mean}, p90 {p90}, max {max})".format(
            count=tools.get("count", 0),
            total=format_duration(tools.get("total")),
            mean=format_duration(tools.get("mean")),
            p90=format_duration(tools.get("p90")),
            max=format_duration(tools.get("max")),
        )
    )
    lines.append(
        "- Slow commands (at least {threshold}): {count}".format(
            threshold=format_duration(threshold), count=len(slow)
        )
    )
    longest = (stats.get("longest_commands") or [])[:3]
    if longest:
        lines.append("")
        lines.append("Longest commands:")
        lines.append("")
        for row in longest:
            command = str(row.get("command") or "")
            if not row.get("slow"):
                command = _bound_preview(command) or ""
            command = _cell(command)
            lines.append(
                "- {duration} ({tool}, {agent}): `{command}`".format(
                    duration=format_duration(row.get("duration")),
                    tool=_cell(row.get("tool") or "-"),
                    agent=_cell(row.get("agent") or "-"),
                    command=command,
                )
            )
    lines.append("")
    lines.append(f"The complete timing report is `{path}`.")
    lines.append("")
    return lines


__all__ = [
    "CAMPAIGN_AGENTS",
    "format_duration",
    "ingest",
    "parse_transcript",
    "pi_session_path",
    "render",
    "render_section",
    "summarize",
    "transcript_dir",
]
