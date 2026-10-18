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
(``.sliceme/<branch-key>.report.md``): design ref, feature branch, nodes,
worker ids, commits, fingerprints/verifications, and artifact paths, with an
optional narrative appended by the coordinator.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import gitutil
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
    "node_by_id",
    "node_status",
    "pull_request_content",
    "render",
    "report_path",
    "session_path",
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
def _verification_line(verification: dict[str, Any]) -> str:
    source = verification.get("source") or "plane"
    status = verification.get("status")
    fingerprint = (verification.get("fingerprint") or "")[:12]
    return f"{source}: {status} (fp {fingerprint})"


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


def evidence_narrative(skeleton: dict[str, Any]) -> str:
    """The deterministic engine-written narrative of the evidence document.

    The engine writes this summary; there is no narrative agent.  It names the
    commit count, the nodes, and the file change totals.
    """
    commits = skeleton.get("commits") or []
    files = 0
    additions = 0
    deletions = 0
    nodes: list[str] = []
    for commit in commits:
        diffstat = commit.get("diffstat") or {}
        files += int(diffstat.get("files") or 0)
        additions += int(diffstat.get("additions") or 0)
        deletions += int(diffstat.get("deletions") or 0)
        node = commit.get("node")
        if node and node not in nodes:
            nodes.append(str(node))
    nodes_text = ", ".join(nodes) if nodes else "(none)"
    return (
        f"The engine recorded {len(commits)} campaign commit(s) across "
        f"{len(nodes)} node(s): {nodes_text}. The campaign changed {files} file(s), "
        f"with {additions} addition(s) and {deletions} deletion(s)."
    )


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
        per_node.append(
            {
                "node": node_id,
                "label": node.get("label"),
                "status": node_status(desc_nodes, node_id) if dag else None,
                "unit_state": (campaign_row or {}).get("state"),
                "branch": (campaign_row or {}).get("work_branch"),
                "log": str(worker_log_path(root, branch, node_id)),
                "record": int(latest["id"]) if latest is not None else None,
                "record_status": latest["status"] if latest is not None else None,
                "commit": latest["head_commit"] if latest is not None else None,
                "verification": verification,
            }
        )

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
        },
        "report_path": str(report_path(root, branch)),
        "nodes": per_node,
        "commits": _commit_evidence(root, config, store, nodes, records, branch),
    }


def render(skeleton: dict[str, Any], narrative: str | None = None) -> str:
    lines: list[str] = []
    lines.append("# Campaign report: " + str(skeleton.get("campaign") or "(unnamed)"))
    lines.append("")
    lines.append(f"- Design: {skeleton.get('design') or '(unspecified)'}")
    lines.append(f"- Feature branch: {skeleton.get('feature_branch')}")
    lines.append(f"- Base: {skeleton.get('base') or '(unspecified)'}")
    if skeleton.get("concurrency") is not None:
        lines.append(f"- Concurrency: {skeleton['concurrency']}")
    lines.append("")
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
    lines.append("## Verifications")
    lines.append("")
    for node in skeleton.get("nodes", []):
        verification = node.get("verification")
        if verification:
            lines.append(f"- {node['node']}: " + _verification_line(verification))
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
    lines.append("## What changed / risks")
    lines.append("")
    lines.append(narrative.strip() if narrative and narrative.strip() else "(no narrative supplied)")
    lines.append("")
    return "\n".join(lines)


def write_report(
    root: Path,
    config: dict[str, Any],
    store: Store,
    *,
    narrative: str | None = None,
    branch: str | None = None,
    campaign: str | None = None,
    design: str | None = None,
) -> dict[str, Any]:
    branch = branch or config_branch(config)
    skeleton = build_skeleton(
        root, config, store, branch=branch, campaign=campaign, design=design
    )
    content = render(skeleton, narrative)
    path = report_path(root, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"path": str(path), "content": content, "skeleton": skeleton}


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
    output bounded, so the document stays a manageable size.  The Markdown
    document is the pull request body.  The evidence is not a gate.
    """
    branch = branch or config_branch(config)
    skeleton = build_skeleton(
        root, config, store, branch=branch, campaign=campaign, design=design
    )
    content = render(skeleton, evidence_narrative(skeleton))
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


def pull_request_content(
    root: Path,
    config: dict[str, Any],
    store: Store,
    *,
    campaign: str | None = None,
) -> tuple[str, str]:
    """The title and body of the delivery pull request.

    The body is the deterministic evidence document.  When it is missing,
    generate it first.  A short footer names the campaign branch and the
    delivery base.
    """
    branch = config_branch(config)
    path = evidence_md_path(root, branch)
    if not path.is_file():
        write_evidence(root, config, store, branch=branch, campaign=campaign)
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        body = ""
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
