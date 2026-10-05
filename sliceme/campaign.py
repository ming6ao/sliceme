"""Campaign plane state, DAG/state readers, and the deterministic report.

All campaign files live under ``.sliceme/`` and are **prefixed by the
feature-branch name** so one campaign's files form a single glob and no two
campaigns collide.  ``/`` in the branch name is replaced with ``--``::

    feat/nanochat-cpp  ->  feat--nanochat-cpp

The orchestrator (the pi `campaign` extension) owns writing ``dag.json`` and
``state.json``; Python reads them for ``sliceme report`` and resolves their
paths.  ``dag.json`` is plane state, never committed to the repository.

This module also renders the deterministic report skeleton
(``.sliceme/<branch-key>.report.md``): design ref, feature branch, nodes,
worker ids, commits, fingerprints/verifications, and artifact paths, with an
optional narrative appended by the coordinator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


def state_path(root: Path, branch: str) -> Path:
    return state_dir(root) / f"{branch_key(branch)}.state.json"


def report_path(root: Path, branch: str) -> Path:
    return state_dir(root) / f"{branch_key(branch)}.report.md"


def worker_log_path(root: Path, branch: str, node: str) -> Path:
    return state_dir(root) / f"{branch_key(branch)}.worker_{node}.log"


def session_path(root: Path, branch: str) -> Path:
    """The adapter-written suspend/resume descriptor for a campaign."""
    return state_dir(root) / f"{branch_key(branch)}.session.json"


def control_path(root: Path, branch: str) -> Path:
    """The cooperative pause flag (``{"pause": true, ...}``) for a campaign."""
    return state_dir(root) / f"{branch_key(branch)}.control.json"


def heartbeat_path(root: Path, branch: str, node: str) -> Path:
    """The per-node progress heartbeat written by a running subagent."""
    return state_dir(root) / f"{branch_key(branch)}.progress_{node}.json"


def events_path(root: Path, branch: str) -> Path:
    """The append-only audit log (``.events.jsonl``) for a campaign."""
    return state_dir(root) / f"{branch_key(branch)}.events.jsonl"


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


def load_state(root: Path, branch: str) -> dict[str, Any]:
    data = read_json(state_path(root, branch)) or {}
    if not isinstance(data.get("nodes"), dict):
        data["nodes"] = {}
    return data


def node_by_id(dag: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(n["id"]): n for n in dag.get("nodes", []) if n.get("id")}


def node_status(state: dict[str, Any], node: str) -> str:
    entry = (state.get("nodes") or {}).get(node) or {}
    status = entry.get("status") if isinstance(entry, dict) else None
    return str(status or "pending")


def config_branch(config: dict[str, Any]) -> str:
    branch = config.get("main_branch")
    if not branch:
        raise SlicemeError("plane has no main_branch; run `sliceme start` first")
    return str(branch)


__all__ = [
    "branch_key",
    "build_skeleton",
    "config_branch",
    "control_path",
    "dag_path",
    "events_path",
    "heartbeat_path",
    "list_sessions",
    "load_control",
    "load_dag",
    "load_session",
    "load_state",
    "node_by_id",
    "node_status",
    "render",
    "report_path",
    "session_path",
    "state_path",
    "worker_log_path",
    "write_control",
    "write_report",
    "write_session",
]


# ---------------------------------------------------------------------------
# Deterministic campaign report
# ---------------------------------------------------------------------------
def _verification_line(verification: dict[str, Any]) -> str:
    source = verification.get("source") or "plane"
    status = verification.get("status")
    gpu = verification.get("gpu") or "-"
    fingerprint = (verification.get("fingerprint") or "")[:12]
    return f"{source}: {status} (fp {fingerprint}, gpu {gpu})"


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
    state = load_state(root, branch)
    nodes = node_by_id(dag) if dag else {}

    units = sorted(
        store.list_units(campaign=campaign) if campaign else store.list_units(),
        key=lambda u: int(u["id"]),
    )
    candidates = sorted(
        store.list_candidates(campaign=campaign), key=lambda c: int(c["id"])
    )

    per_node: list[dict[str, Any]] = []
    node_candidates = [c for c in candidates if c.get("node")]
    if node_candidates and nodes:
        # Wave scope: candidates carry their node id and share a wave unit.
        units_by_id = {int(unit["id"]): unit for unit in units}
        for node_id, node in nodes.items():
            rows = [c for c in node_candidates if c.get("node") == node_id]
            latest = rows[-1] if rows else None
            unit = units_by_id.get(int(latest["unit_id"])) if latest is not None else None
            verification = (
                store.latest_job_for_commit(str(latest["head_commit"]))
                if latest is not None
                else None
            )
            per_node.append(
                {
                    "node": node_id,
                    "label": node.get("label"),
                    "phase": node.get("phase"),
                    "status": node_status(state, node_id) if dag else None,
                    "unit_state": unit["state"] if unit else None,
                    "branch": (unit or {}).get("branch"),
                    "log": str(worker_log_path(root, branch, node_id)),
                    "candidate": int(latest["id"]) if latest is not None else None,
                    "candidate_status": latest["status"] if latest is not None else None,
                    "commit": latest["head_commit"] if latest is not None else None,
                    "verification": verification,
                }
            )
    else:
        for unit in units:
            name = str(unit["name"])
            node = nodes.get(name)
            unit_candidates = [c for c in candidates if int(c["unit_id"]) == int(unit["id"])]
            latest = unit_candidates[-1] if unit_candidates else None
            verification = (
                store.latest_job_for_commit(str(latest["head_commit"]))
                if latest is not None
                else None
            )
            per_node.append(
                {
                    "node": name,
                    "label": (node or {}).get("label"),
                    "phase": (node or {}).get("phase"),
                    "status": node_status(state, name) if dag else unit["state"],
                    "unit_state": unit["state"],
                    "branch": unit["branch"],
                    "log": str(worker_log_path(root, branch, name)),
                    "candidate": int(latest["id"]) if latest is not None else None,
                    "candidate_status": latest["status"] if latest is not None else None,
                    "commit": latest["head_commit"] if latest is not None else None,
                    "verification": verification,
                }
            )

    return {
        "campaign": (dag or {}).get("campaign") or config.get("campaign", {}).get("name"),
        "design": design or (dag or {}).get("design") or config.get("campaign", {}).get("design"),
        "feature_branch": branch,
        "base": (dag or {}).get("base") or config.get("base"),
        "concurrency": (dag or {}).get("concurrency"),
        "artifact_paths": {
            "dag": str(dag_path(root, branch)),
            "state": str(state_path(root, branch)),
            "report": str(report_path(root, branch)),
        },
        "nodes": per_node,
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
    lines.append("| node | phase | status | unit | branch | candidate | commit | verification |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for node in skeleton.get("nodes", []):
        verification = node.get("verification") or {}
        verification_text = (
            f"{verification.get('source') or 'plane'}:{verification.get('status')}"
            if verification
            else "-"
        )
        commit = node.get("commit") or "-"
        lines.append(
            "| {node} | {phase} | {status} | {unit} | {branch} | {candidate} | {commit} | {verification} |".format(
                node=node["node"],
                phase=node.get("phase") or "-",
                status=node.get("status"),
                unit=node.get("unit_state"),
                branch=node.get("branch"),
                candidate=node.get("candidate") or "-",
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
