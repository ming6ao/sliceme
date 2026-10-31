"""Campaign plane state, DAG/state readers, and the deterministic report.

All campaign files live under ``.sliceme/`` and are **prefixed by the
feature-branch name** so one campaign's files form a single glob and no two
campaigns collide.  ``/`` in the branch name is replaced with ``--``::

    feat/nanochat-cpp  ->  feat--nanochat-cpp

The orchestrator (the pi extension) owns writing ``dag.json``; Python reads
it for ``sliceme report`` and resolves its path.  ``dag.json`` is plane state,
never committed to the repository.  The adapter-written session descriptor is
the one place a suspended node's status lives; there is no ``state.json``.

This module also renders the deterministic report skeleton
(``.sliceme/<branch-key>.report.md``) and the body of the delivery pull
request.  Both documents lead with a summary and a verification list.  The
report then carries every detail: the design ref, the feature branch, the
nodes, the commits, the fingerprints, and the artifact paths.  The pull
request body carries only the summary, the per-commit change rows, and the
checks, because a reviewer reads the diff for the rest.  The optional
narrative is the coordinator's prose under the summary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import gitutil
from . import timings
from .ownership import node_dir
from .store import Store
from .util import (
    SlicemeError,
    branch_key as _branch_key,
    read_json,
    state_dir,
    write_json,
)


def branch_key(branch: str) -> str:
    """The file prefix for a feature branch (``feat/x`` -> ``feat--x``)."""
    return _branch_key(branch)


def dag_path(root: Path, branch: str) -> Path:
    return state_dir(root) / f"{branch_key(branch)}.dag.json"


def report_path(root: Path, branch: str) -> Path:
    return state_dir(root) / f"{branch_key(branch)}.report.md"


def timing_path(root: Path, branch: str) -> Path:
    """The dedicated timing statistics report for a campaign."""
    return state_dir(root) / f"{branch_key(branch)}.timings.md"


def evidence_json_path(root: Path, branch: str) -> Path:
    """The complete evidence document (the full check output)."""
    return state_dir(root) / f"{branch_key(branch)}.evidence.json"


def evidence_md_path(root: Path, branch: str) -> Path:
    """The human evidence document (the check output is bounded)."""
    return state_dir(root) / f"{branch_key(branch)}.evidence.md"


def worker_log_path(root: Path, branch: str, node: str) -> Path:
    return state_dir(root) / f"{branch_key(branch)}.worker_{node}.log"


def session_path(root: Path, branch: str) -> Path:
    """The adapter-written suspend/resume descriptor for a campaign."""
    return state_dir(root) / f"{branch_key(branch)}.session.json"


def control_path(root: Path, branch: str) -> Path:
    """The cooperative pause flag (``{"pause": true, ...}``) for a campaign."""
    return state_dir(root) / f"{branch_key(branch)}.control.json"


def load_session(root: Path, branch: str) -> dict[str, Any] | None:
    data = read_json(session_path(root, branch))
    return data if isinstance(data, dict) else None


def write_session(root: Path, branch: str, descriptor: dict[str, Any]) -> Path:
    path = session_path(root, branch)
    write_json(path, descriptor)
    return path


def load_control(root: Path, branch: str) -> dict[str, Any] | None:
    data = read_json(control_path(root, branch))
    return data if isinstance(data, dict) else None


def budget_path(root: Path, branch: str) -> Path:
    """The adapter-written budget spend file for a campaign.

    The pi adapter owns this file.  It records the active seconds and the
    spent cost of the current campaign, so a resume continues the same budget.
    """
    return state_dir(root) / f"{branch_key(branch)}.budget.json"


def load_budget(root: Path, branch: str) -> dict[str, Any] | None:
    data = read_json(budget_path(root, branch))
    return data if isinstance(data, dict) else None


def write_budget(root: Path, branch: str, spend: dict[str, Any]) -> Path:
    path = budget_path(root, branch)
    write_json(path, spend)
    return path


def write_control(root: Path, branch: str, control: dict[str, Any] | None) -> Path:
    path = control_path(root, branch)
    if control is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return path
    write_json(path, control)
    return path


def list_sessions(root: Path) -> list[tuple[str, dict[str, Any]]]:
    """Every readable ``<branch-key>.session.json``, newest activity first.

    Returns ``(branch_key, descriptor)`` pairs so a caller can map a descriptor
    back to the campaign path even when the descriptor omits its branch.
    """
    found: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(state_dir(root).glob("*.session.json")):
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        key = path.name[: -len(".session.json")]
        found.append((key, data))
    found.sort(
        key=lambda item: float(item[1].get("suspended_at") or item[1].get("updated_at") or 0.0),
        reverse=True,
    )
    return found


def load_dag(root: Path, branch: str) -> dict[str, Any] | None:
    return read_json(dag_path(root, branch))


def descriptor_nodes(descriptor: dict[str, Any] | None) -> dict[str, Any]:
    """The node-status map the adapter recorded in a session descriptor."""
    nodes = (descriptor or {}).get("nodes")
    return nodes if isinstance(nodes, dict) else {}


def node_by_id(dag: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(n["id"]): n for n in dag.get("nodes", []) if n.get("id")}


def node_status(nodes: dict[str, Any], node: str) -> str:
    """One node's status from a descriptor node map (string or object entry)."""
    entry = nodes.get(node)
    if isinstance(entry, dict):
        return str(entry.get("status") or "pending")
    if isinstance(entry, str) and entry:
        return entry
    return "pending"


def merge_node_status(
    desc_nodes: dict[str, Any], recorded: set[str], node: str
) -> str:
    """One node's report status: a record wins over the descriptor.

    A record is durable evidence that the node finished, so a stale descriptor
    cannot show a recorded node as pending or running.  The report reads the
    descriptor for a node with no record, because a suspended node's
    ``running`` status lives there and nowhere else.
    """
    if node in recorded:
        return "recorded"
    return node_status(desc_nodes, node)


def config_branch(config: dict[str, Any]) -> str:
    branch = config.get("work_branch") or config.get("main_branch")
    if not branch:
        raise SlicemeError("plane has no work_branch; run `sliceme start` first")
    return str(branch)


__all__ = [
    "branch_key",
    "budget_path",
    "build_skeleton",
    "config_branch",
    "control_path",
    "dag_path",
    "descriptor_nodes",
    "evidence_json_path",
    "evidence_md_path",
    "evidence_narrative",
    "list_sessions",
    "load_budget",
    "load_control",
    "load_dag",
    "load_session",
    "merge_node_status",
    "node_by_id",
    "node_status",
    "pull_request_content",
    "render",
    "render_pull_request",
    "report_path",
    "session_path",
    "timing_path",
    "worker_log_path",
    "write_budget",
    "write_control",
    "write_evidence",
    "write_report",
    "write_session",
]


# ---------------------------------------------------------------------------
# Deterministic campaign report
# ---------------------------------------------------------------------------
def _commands_text(commands: Any) -> str:
    """One check command vector as a readable ``a && b`` line."""
    if isinstance(commands, str):
        commands = _json_value(commands)
    if isinstance(commands, list):
        return " && ".join(str(item) for item in commands)
    return str(commands) if commands else ""


def _check_evidence_line(check: dict[str, Any]) -> str:
    fingerprint = str(check.get("fingerprint") or "")[:12]
    duration = check.get("duration")
    duration_text = f"{duration}s" if duration is not None else "?s"
    source = check.get("source") or "plane"
    return f"{check.get('status')} ({source}, fp {fingerprint}, {duration_text})"


def _bounded_output(output: Any) -> str:
    """The check output for the Markdown document, bounded by packet.py."""
    from .review import packet  # lazy: packet imports this module

    return packet.bound_output(str(output or ""))


def _count(number: int, noun: str) -> str:
    """One count with its noun, pluralized by the count."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def evidence_narrative(skeleton: dict[str, Any]) -> str:
    """The deterministic summary paragraph of the campaign documents.

    The engine writes this summary; there is no narrative agent.  It names the
    design, the commit count, the node count, and the whole-campaign change
    totals.  The ``What changed`` rows carry the detail, and the optional
    narrative carries the coordinator's prose.
    """
    commits = skeleton.get("commits") or []
    totals = skeleton.get("totals") or {}
    nodes: list[str] = []
    for commit in commits:
        node = commit.get("node")
        if node and node not in nodes:
            nodes.append(str(node))
    sentences: list[str] = []
    design = skeleton.get("design")
    if design:
        sentences.append(f"The campaign implements `{design}`.")
    sentences.append(
        f"The campaign records {_count(len(commits), 'commit')} across "
        f"{_count(len(nodes), 'node')}."
    )
    if totals:
        sentences.append(
            "The change set is {files} (+{additions} -{deletions}).".format(
                files=_count(int(totals.get("files") or 0), "file"),
                additions=int(totals.get("additions") or 0),
                deletions=int(totals.get("deletions") or 0),
            )
        )
    return " ".join(sentences)


def _duration_text(duration: Any) -> str:
    """One check duration as a short ``N.Ns`` text."""
    try:
        return f"{float(duration):.1f}s"
    except (TypeError, ValueError):
        return "?s"


def _command_list(commands: Any) -> list[str]:
    """One check command vector as a list of command strings."""
    if isinstance(commands, str):
        commands = _json_value(commands)
    if isinstance(commands, list):
        return [str(item) for item in commands if str(item).strip()]
    return [str(commands)] if commands else []


def _split_subject(subject: str) -> tuple[str | None, str]:
    """Split a commit subject ``dir: text`` into the directory and the text.

    A worker names its directory first.  The prefix counts as a directory only
    when it holds no space, so an ordinary sentence keeps its whole subject.
    """
    text = " ".join(str(subject or "").split())
    head, sep, tail = text.partition(": ")
    if sep and head and " " not in head and len(head) <= 60:
        return head, tail
    return None, text


#: The worker-log lines that the evidence document keeps from the log end.
LOG_TAIL_LINES = 40


def _rev(root: Path, ref: str | None) -> str | None:
    if not ref:
        return None
    try:
        return gitutil.rev_parse(root, ref)
    except SlicemeError:
        return None


def _json_value(raw: Any) -> Any:
    """Parse a store text column that holds JSON; keep a non-JSON value as is."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def _check_evidence(check: dict[str, Any] | None) -> dict[str, Any] | None:
    """One check row as evidence: the status, the command, the fingerprint, the
    duration, and the full output."""
    if not check:
        return None
    return {
        "status": check.get("status"),
        "commands": _json_value(check.get("commands")),
        "fingerprint": check.get("fingerprint"),
        "duration": check.get("duration"),
        "output": check.get("output"),
        "source": check.get("source"),
        "created_at": check.get("finished_at") or check.get("created_at"),
    }


def _log_evidence(path: str | None) -> dict[str, Any]:
    """The worker log path and the tail of the log (empty when absent)."""
    if not path:
        return {"path": None, "tail": ""}
    tail = ""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        tail = "\n".join(text.splitlines()[-LOG_TAIL_LINES:])
    except OSError:
        tail = ""
    return {"path": path, "tail": tail}


def _commit_evidence(
    root: Path,
    config: dict[str, Any],
    store: Store,
    nodes: dict[str, dict[str, Any]],
    records: list[dict[str, Any]],
    branch: str,
) -> list[dict[str, Any]]:
    """One evidence row for each campaign commit, oldest first.

    The rows cover the commits the campaign worktree adds on top of the
    delivery base.  Each row holds the node, the goal, the owned directories,
    the newest check, the diffstat, the changed files, and the worker log tail.
    """
    from .review import diff, packet  # lazy: packet imports this module

    delivery_base = config.get("base_branch") or config.get("default_branch") or "main"
    source = config.get("work_branch") or branch
    source_tip = _rev(root, source)
    target_tip = _rev(root, delivery_base)
    if not source_tip or not target_tip:
        return []
    node_of_commit: dict[str, str] = {}
    for record in records:
        head = str(record.get("head_commit") or "")
        node_id = str(record.get("node") or "")
        if head and node_id and head not in node_of_commit:
            node_of_commit[head] = node_id
    rows: list[dict[str, Any]] = []
    for commit in diff.commits(root, target_tip, source_tip):
        hash_ = commit["hash"]
        node_id = node_of_commit.get(hash_)
        node = nodes.get(node_id) if node_id else None
        changes = packet.commit_changes(root, hash_)
        log_path = worker_log_path(root, branch, node_id) if node_id else None
        rows.append(
            {
                "hash": hash_,
                "short": commit.get("short"),
                "subject": commit.get("subject"),
                "author": commit.get("author"),
                "date": commit.get("date"),
                "node": node_id,
                "goal": (node or {}).get("goal"),
                "dir": node_dir(node) if node else None,
                "tasks": list((node or {}).get("tasks") or []),
                "check": _check_evidence(store.latest_verdict_for_commit(hash_)),
                "diffstat": changes["diffstat"],
                "files": changes["files"],
                "log": _log_evidence(str(log_path) if log_path else None),
            }
        )
    return rows


def _totals(root: Path, config: dict[str, Any], branch: str) -> dict[str, Any]:
    """The whole-campaign change totals, from one diff against the base.

    A sum of per-commit diffstats counts a file twice when two nodes touch it.
    One diff of the delivery base against the campaign tip is the true total.
    """
    from .review import diff, packet  # lazy: packet imports this module

    base = config.get("base_branch") or config.get("default_branch") or "main"
    source = config.get("work_branch") or branch
    base_tip = _rev(root, base)
    source_tip = _rev(root, source)
    if not base_tip or not source_tip:
        return {}
    return packet.diffstat(diff.file_index(root, base_tip, source_tip))


def _phase_checks(
    store: Store, commits: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One verification row for each check the campaign ran, oldest first.

    Only a verdict whose commit is a campaign commit counts, so a report never
    shows another campaign's check.  The commit filter is the whole test: an
    older plane wrote a verdict before the campaign column existed, and a
    commit belongs to one campaign only.  A repeated check of one phase
    replaces the earlier row, because the newest terminal verdict is the
    evidence.  The ``covered`` count is how many campaign commits the checked
    tree contains.
    """
    from .store import VERDICT_STATUSES

    index_of = {str(row.get("hash")): i for i, row in enumerate(commits)}
    newest: dict[tuple[Any, Any], dict[str, Any]] = {}
    for row in store.list_verdicts(statuses=sorted(VERDICT_STATUSES)):
        commit = str(row.get("commit_ref") or "")
        if commit not in index_of:
            continue
        newest[(row.get("phase"), row.get("source"))] = {
            "phase": row.get("phase"),
            "source": row.get("source"),
            "status": row.get("status"),
            "duration": row.get("duration"),
            "fingerprint": row.get("fingerprint"),
            "commands": _json_value(row.get("commands")),
            "commit": commit,
            "covered": index_of[commit] + 1,
        }
    rows = list(newest.values())
    rows.sort(key=lambda item: (item.get("phase") is None, item.get("phase") or 0))
    return rows


def build_skeleton(
    root: Path,
    config: dict[str, Any],
    store: Store,
    *,
    branch: str | None = None,
    campaign: str | None = None,
    design: str | None = None,
) -> dict[str, Any]:
    branch = branch or config_branch(config)
    dag = load_dag(root, branch)
    nodes = node_by_id(dag) if dag else {}
    # The descriptor is the one place a suspended node's status lives; the
    # engine never writes a separate state file.
    desc_nodes = descriptor_nodes(load_session(root, branch))

    campaign_row = store.get_campaign(campaign) if campaign else None
    records = sorted(
        store.list_records(campaign=campaign), key=lambda c: int(c["id"])
    )

    per_node: list[dict[str, Any]] = []
    # A record is the durable evidence that a node finished.  The descriptor
    # supplies the status only for a node with no record, so a stale descriptor
    # cannot hide a recorded node.
    recorded = {str(c["node"]) for c in records if c.get("node")}
    # The campaign owns the worktree and the delivery state, so the report
    # reads the campaign row rather than a retired unit row.
    for node_id, node in nodes.items():
        # An empty marker is not a commit: its stored head is another node's
        # commit, so it must not be shown as this node's commit or check.
        rows = [
            c
            for c in records
            if c.get("node") == node_id and not c.get("empty")
        ]
        latest = rows[-1] if rows else None
        verification = (
            store.latest_verdict_for_commit(str(latest["head_commit"]))
            if latest is not None
            else None
        )
        status = merge_node_status(desc_nodes, recorded, node_id) if dag else None
        per_node.append(
            {
                "node": node_id,
                "label": node.get("label"),
                "status": status,
                "unit_state": (campaign_row or {}).get("state"),
                "branch": (campaign_row or {}).get("work_branch"),
                "log": str(worker_log_path(root, branch, node_id)),
                "record": int(latest["id"]) if latest is not None else None,
                "record_status": latest["status"] if latest is not None else None,
                "commit": latest["head_commit"] if latest is not None else None,
                "verification": verification,
            }
        )

    commits = _commit_evidence(root, config, store, nodes, records, branch)
    return {
        "campaign": (dag or {}).get("campaign") or config.get("campaign", {}).get("name"),
        "design": design or (dag or {}).get("design") or config.get("campaign", {}).get("design"),
        "feature_branch": branch,
        "base": (dag or {}).get("base") or config.get("base_branch"),
        "delivery_base": config.get("base_branch") or config.get("default_branch") or "main",
        "concurrency": (dag or {}).get("concurrency"),
        "artifact_paths": {
            "dag": str(dag_path(root, branch)),
            "report": str(report_path(root, branch)),
            "evidence_json": str(evidence_json_path(root, branch)),
            "evidence_md": str(evidence_md_path(root, branch)),
            "timings": str(timing_path(root, branch)),
        },
        "report_path": str(report_path(root, branch)),
        "nodes": per_node,
        "commits": commits,
        "totals": _totals(root, config, branch),
        "checks": _phase_checks(store, commits),
    }


def _summary_block(skeleton: dict[str, Any], narrative: str | None) -> list[str]:
    """The leading summary: the deterministic digest, then the narrative."""
    lines = ["## Summary", "", evidence_narrative(skeleton), ""]
    if narrative and narrative.strip():
        lines.append(narrative.strip())
        lines.append("")
    return lines


def _directory_label(directory: Any) -> str | None:
    """A node directory as a display label; the repository root is ``None``."""
    text = str(directory or "").strip().strip("/")
    if text in {"", ".", "./"}:
        return None
    return text


def _changes_block(skeleton: dict[str, Any]) -> list[str]:
    """One row per campaign commit: the directory, the subject, the diffstat."""
    lines = ["## What changed", ""]
    commits = skeleton.get("commits") or []
    if not commits:
        lines.extend(["(no campaign commits)", ""])
        return lines
    for commit in commits:
        short = commit.get("short") or str(commit.get("hash") or "")[:12]
        directory = _directory_label(commit.get("dir"))
        subject = str(commit.get("subject") or "")
        prefix, tail = _split_subject(subject)
        names_the_directory = bool(prefix) and (
            directory is None
            or prefix == directory
            or prefix.startswith(directory + "/")
        )
        if names_the_directory:
            label, text = directory or prefix or "", tail
        else:
            label, text = directory or prefix or "(repository root)", subject
        if not text:
            text = "(no subject)"
        diffstat = commit.get("diffstat") or {}
        lines.append(
            "- `{short}` — **{label}**: {text} ("
            "{files}, +{additions} -{deletions})".format(
                short=short,
                label=label,
                text=text,
                files=_count(int(diffstat.get("files") or 0), "file"),
                additions=int(diffstat.get("additions") or 0),
                deletions=int(diffstat.get("deletions") or 0),
            )
        )
    lines.append("")
    return lines


def _verification_block(skeleton: dict[str, Any]) -> list[str]:
    """One row per recorded check: the status, the commands, the coverage."""
    lines = ["## Verification", ""]
    checks = skeleton.get("checks") or []
    commits = skeleton.get("commits") or []
    if not checks:
        lines.extend(["(no check was recorded)", ""])
        return lines
    covered = 0
    for check in checks:
        phase = check.get("phase")
        where = f"Phase {phase}" if phase is not None else str(
            check.get("source") or "check"
        )
        parts = [f"- **{where}**: {check.get('status')}"]
        if check.get("duration") is not None:
            parts.append(f"in {_duration_text(check.get('duration'))}")
        fingerprint = str(check.get("fingerprint") or "")[:12]
        if fingerprint:
            parts.append(f"(fingerprint `{fingerprint}`)")
        count = int(check.get("covered") or 0)
        covered = max(covered, count)
        if count == 1:
            parts.append("covering commit 1")
        elif count > 1:
            parts.append(f"covering commits 1-{count}")
        lines.append(" ".join(parts))
        for command in _command_list(check.get("commands")):
            lines.append(f"  - `{command}`")
    missing = commits[covered:]
    if missing:
        names = [
            f"`{row.get('short') or str(row.get('hash') or '')[:12]}` {row.get('subject') or ''}".strip()
            for row in missing
        ]
        lines.append("")
        lines.append("No check covers these commits:")
        lines.append("")
        for name in names:
            lines.append(f"- {name}")
    lines.append("")
    return lines


def render(
    skeleton: dict[str, Any],
    narrative: str | None = None,
    *,
    timing: dict[str, Any] | None = None,
    timing_file: str | None = None,
) -> str:
    """The campaign report: the summary first, then the complete detail."""
    lines: list[str] = []
    lines.append("# Campaign report: " + str(skeleton.get("campaign") or "(unnamed)"))
    lines.append("")
    lines.append(f"- Design: {skeleton.get('design') or '(unspecified)'}")
    lines.append(f"- Feature branch: {skeleton.get('feature_branch')}")
    lines.append(f"- Base: {skeleton.get('base') or '(unspecified)'}")
    if skeleton.get("concurrency") is not None:
        lines.append(f"- Concurrency: {skeleton['concurrency']}")
    lines.append("")
    lines.extend(_summary_block(skeleton, narrative))
    lines.extend(_changes_block(skeleton))
    lines.extend(_verification_block(skeleton))
    if timing is not None:
        lines.extend(
            timings.render_section(
                timing,
                path=timing_file or str(skeleton.get("artifact_paths", {}).get("timings")),
            )
        )
    lines.append("## Nodes")
    lines.append("")
    lines.append("| node | status | unit | branch | record | commit | verification |")
    lines.append("|---|---|---|---|---|---|---|")
    for node in skeleton.get("nodes", []):
        verification = node.get("verification") or {}
        verification_text = (
            f"{verification.get('source') or 'plane'}:{verification.get('status')}"
            if verification
            else "-"
        )
        commit = node.get("commit") or "-"
        lines.append(
            "| {node} | {status} | {unit} | {branch} | {record} | {commit} | {verification} |".format(
                node=node["node"],
                status=node.get("status"),
                unit=node.get("unit_state"),
                branch=node.get("branch"),
                record=node.get("record") or "-",
                commit=commit[:12],
                verification=verification_text,
            )
        )
    lines.append("")
    lines.append("## Evidence")
    lines.append("")
    commits = skeleton.get("commits") or []
    if not commits:
        lines.append("(no campaign commits)")
        lines.append("")
    for commit in commits:
        short = commit.get("short") or str(commit.get("hash") or "")[:12]
        subject = commit.get("subject") or ""
        lines.append(f"### {short} {subject}".rstrip())
        lines.append("")
        lines.append(f"- Node: {commit.get('node') or '-'}")
        lines.append(f"- Goal: {commit.get('goal') or '-'}")
        lines.append(f"- Directory: {commit.get('dir') or '-'}")
        tasks = commit.get("tasks") or []
        if tasks:
            lines.append("- Tasks: " + "; ".join(str(task) for task in tasks))
        check = commit.get("check")
        if check:
            lines.append(f"- Check: {_check_evidence_line(check)}")
            command = _commands_text(check.get("commands"))
            if command:
                lines.append(f"  - command: {command}")
            output = _bounded_output(check.get("output"))
            if output.strip():
                lines.append("  - output:")
                for line in output.splitlines():
                    lines.append("    " + line)
            else:
                lines.append("  - output: (empty)")
        else:
            lines.append("- Check: (none)")
        diffstat = commit.get("diffstat") or {}
        lines.append(
            "- Diffstat: {files} file(s), +{additions} -{deletions}".format(
                files=diffstat.get("files", 0),
                additions=diffstat.get("additions", 0),
                deletions=diffstat.get("deletions", 0),
            )
        )
        files = commit.get("files") or []
        if files:
            lines.append("- Changed files:")
            for row in files:
                lines.append(
                    "  - {status} {path} (+{additions} -{deletions})".format(
                        status=row.get("status") or "?",
                        path=row.get("path"),
                        additions=row.get("additions", 0),
                        deletions=row.get("deletions", 0),
                    )
                )
        log = commit.get("log") or {}
        lines.append(f"- Worker log: {log.get('path') or '-'}")
        tail = log.get("tail")
        if tail:
            for line in str(tail).splitlines():
                lines.append("    " + line)
        lines.append("")
    lines.append("## Artifacts")
    lines.append("")
    for name, path in sorted((skeleton.get("artifact_paths") or {}).items()):
        lines.append(f"- {name}: {path}")
    lines.append("")
    return "\n".join(lines)


def _timing_threshold(config: dict[str, Any], override: float | None) -> float:
    """The slow-command threshold: the flag, then the plane config, then 120s."""
    if override is not None:
        try:
            value = float(override)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    declared_block = config.get("timings")
    declared = (
        declared_block.get("slow_seconds")
        if isinstance(declared_block, dict)
        else None
    )
    try:
        value = float(declared)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return timings.SLOW_SECONDS


def write_report(
    root: Path,
    config: dict[str, Any],
    store: Store,
    *,
    narrative: str | None = None,
    branch: str | None = None,
    campaign: str | None = None,
    design: str | None = None,
    slow_seconds: float | None = None,
) -> dict[str, Any]:
    branch = branch or config_branch(config)
    key = campaign
    if key is None:
        row = store.get_campaign(branch)
        key = str(row["key"]) if row else None
    threshold = _timing_threshold(config, slow_seconds)
    rows = store.list_timings(campaign=key) if key else []
    timing = timings.summarize(rows, slow_seconds=threshold)
    campaign_row = (store.get_campaign(key) if key else None) or {}
    timing_doc = timings.render(
        timing,
        campaign=str(campaign_row.get("name") or branch),
        branch=branch,
        worktree=str(campaign_row.get("worktree") or "") or None,
        slow_seconds=threshold,
    )
    file = timing_path(root, branch)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(timing_doc, encoding="utf-8")
    skeleton = build_skeleton(
        root, config, store, branch=branch, campaign=campaign, design=design
    )
    content = render(
        skeleton,
        narrative,
        timing=timing,
        timing_file=str(file),
    )
    path = report_path(root, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {
        "path": str(path),
        "content": content,
        "skeleton": skeleton,
        "timing": {
            "path": str(file),
            "content": timing_doc,
            "summary": timing,
        },
    }


def render_pull_request(
    skeleton: dict[str, Any], narrative: str | None = None
) -> str:
    """The body of the delivery pull request.

    The body answers two reviewer questions: what changed, and how the campaign
    verified it.  It carries no goals, no worker logs, no local paths, and no
    check output.  The complete evidence stays in the local evidence document,
    because a reviewer reads the diff for the detail.
    """
    lines = _summary_block(skeleton, narrative)
    lines.extend(_changes_block(skeleton))
    lines.extend(_verification_block(skeleton))
    return "\n".join(lines).rstrip() + "\n"


def _write_evidence(
    root: Path, branch: str, skeleton: dict[str, Any]
) -> dict[str, Any]:
    """Write both evidence files for one already-built skeleton.

    The digest is the summary of the document itself, so the evidence carries
    no narrative: the narrative belongs to the report and the pull request.
    """
    content = render(skeleton)
    json_path = evidence_json_path(root, branch)
    md_path = evidence_md_path(root, branch)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(json_path, skeleton)
    md_path.write_text(content, encoding="utf-8")
    return {
        "path": str(md_path),
        "json_path": str(json_path),
        "content": content,
        "evidence": skeleton,
    }


def write_evidence(
    root: Path,
    config: dict[str, Any],
    store: Store,
    *,
    branch: str | None = None,
    campaign: str | None = None,
    design: str | None = None,
) -> dict[str, Any]:
    """Write the deterministic evidence document for a campaign.

    ``.sliceme/<key>.evidence.json`` holds the complete evidence: the full
    check output, the changed files, and the diffstat for every commit.
    ``.sliceme/<key>.evidence.md`` holds the same evidence with the check
    output bounded, so the document stays a manageable size.  The evidence is
    the local record of the campaign, and it is not a gate.  The pull request
    body is the shorter summary document (:func:`render_pull_request`).
    """
    branch = branch or config_branch(config)
    skeleton = build_skeleton(
        root, config, store, branch=branch, campaign=campaign, design=design
    )
    return _write_evidence(root, branch, skeleton)


def pull_request_content(
    root: Path,
    config: dict[str, Any],
    store: Store,
    *,
    campaign: str | None = None,
    narrative: str | None = None,
) -> tuple[str, str]:
    """The title and body of the delivery pull request.

    The body is the summary and the verification (:func:`render_pull_request`),
    with the coordinator's narrative when it passed one.  Delivery also
    refreshes the local evidence document, so the complete record stays beside
    the campaign.  A short footer names the campaign branch and the delivery
    base.
    """
    branch = config_branch(config)
    skeleton = build_skeleton(root, config, store, branch=branch, campaign=campaign)
    _write_evidence(root, branch, skeleton)
    body = render_pull_request(skeleton, narrative)
    name = (
        config.get("campaign_name")
        or (load_dag(root, branch) or {}).get("campaign")
        or config.get("campaign_key")
        or branch
    )
    title = f"sliceme: {name}"
    delivery_base = (
        config.get("base_branch") or config.get("default_branch") or "main"
    )
    footer = (
        "\n---\n\n"
        f"Campaign branch: `{branch}`\n\n"
        f"Delivery base: `{delivery_base}`\n"
    )
    return title, body.rstrip() + "\n" + footer
