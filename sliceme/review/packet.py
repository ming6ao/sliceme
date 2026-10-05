"""Build one review snapshot: the accumulated commits and the report.

The review unit is the campaign worktree.  Commits accumulate as waves land, so
a human can review and approve them one by one, before or after the campaign
finishes.  An approval is stored per commit hash; delivery proceeds only when
every commit in the packet has a newest unconsumed ``approve``.

The report (``.sliceme/<branch-key>.report.md``) is git-ignored, so it is read
from disk and included in the packet as a virtual file.  Comments can attach to
it.  It is evidence for the reviewer, not a merge gate.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from .. import campaign, gitutil
from ..util import SlicemeError, now
from . import diff

if TYPE_CHECKING:  # pragma: no cover
    from ..service import Service

__all__ = [
    "build_packet",
    "campaign_branch_key",
    "campaign_commits",
    "commit_hashes",
    "file_content",
    "file_lines",
    "report_info",
    "review_commits",
]


def campaign_branch_key(service: "Service") -> str:
    config = service.config
    branch = str(config.get("target_branch") or config.get("main_branch") or "main")
    return campaign.branch_key(branch)


def _target_branch(service: "Service") -> str:
    config = service.config
    return str(config.get("target_branch") or config.get("main_branch") or "main")


def _worktree_branch(service: "Service") -> str | None:
    branch = service.config.get("worktree_branch")
    return str(branch) if branch else None


def _rev(service: "Service", ref: str | None) -> str | None:
    if not ref:
        return None
    try:
        return gitutil.rev_parse(service.root, ref)
    except SlicemeError:
        return None


def _parent(service: "Service", commit: str) -> str | None:
    res = gitutil.git(service.root, "rev-parse", f"{commit}^", check=False)
    return res.stdout.strip() if res.ok else None


def commit_hashes(service: "Service", target: str, source: str) -> list[str]:
    """Every commit the campaign adds on top of the target, oldest first."""
    return [row["hash"] for row in diff.commits(service.root, target, source)]


def campaign_commits(service: "Service") -> list[str]:
    """Every accumulated campaign commit, oldest first."""
    source_tip = _rev(service, _worktree_branch(service))
    target_tip = _rev(service, _target_branch(service))
    if not source_tip or not target_tip:
        return []
    return commit_hashes(service, target_tip, source_tip)


def review_commits(service: "Service") -> list[str]:
    """Campaign commits plus any prepared candidate commits, oldest first."""
    commits = campaign_commits(service)
    seen = set(commits)
    for candidate in service.store.list_candidates(statuses=["prepared"]):
        try:
            head = gitutil.rev_parse(service.root, candidate["unit_branch"])
        except SlicemeError:
            continue
        if head not in seen:
            seen.add(head)
            commits.append(head)
    return commits


def report_info(service: "Service") -> dict[str, Any]:
    """The generated report as a virtual, git-ignored file."""
    branch = _target_branch(service)
    path = campaign.report_path(service.root, branch)
    if not path.is_file():
        return {"path": str(path), "exists": False, "content": "", "updated_at": None}
    try:
        content = path.read_text(encoding="utf-8")
        updated_at = path.stat().st_mtime
    except OSError:
        return {"path": str(path), "exists": False, "content": "", "updated_at": None}
    return {
        "path": str(path),
        "exists": True,
        "content": content,
        "updated_at": updated_at,
    }


def _decision_state(
    decisions: dict[str, dict[str, Any]], commit_hash: str
) -> dict[str, Any]:
    decision = decisions.get(commit_hash)
    approved = bool(
        decision
        and decision.get("action") == "approve"
        and decision.get("consumed_at") is None
    )
    return {"approved": approved, "decision": decision}


def build_packet(service: "Service", *, commit: str | None = None) -> dict[str, Any]:
    """One coherent review snapshot for the client to poll."""
    target = _target_branch(service)
    source_ref = _worktree_branch(service)
    source_tip = _rev(service, source_ref)
    target_tip = _rev(service, target)
    branch_key = campaign_branch_key(service)

    commits: list[dict[str, Any]] = []
    decisions: dict[str, dict[str, Any]] = {}
    if source_tip and target_tip:
        commits = diff.commits(service.root, target_tip, source_tip)
        decisions = service.store.latest_decisions_by_commit(branch_key)
    for row in commits:
        row.update(_decision_state(decisions, row["hash"]))

    base, head = target_tip, source_tip
    if commit:
        parent = _parent(service, commit)
        if parent:
            base = parent
        head = commit

    return {
        "branch_key": branch_key,
        "feature_branch": target,
        "source_tip": source_tip,
        "target_tip": target_tip,
        "commit": commit,
        "commits": commits,
        "all_approved": bool(commits) and all(row["approved"] for row in commits),
        "override": service.store.latest_review_decision(branch_key, None),
        "files": diff.file_index(service.root, base, head) if base and head else [],
        "comments": service.store.list_comments(branch_key=branch_key),
        "evidence": _evidence_map(service, {row["hash"] for row in commits}),
        "report": report_info(service),
        "updated_at": now(),
    }


def _evidence_map(service: "Service", hashes: set[str]) -> dict[str, dict[str, Any]]:
    """The newest verification for each reviewed commit, keyed by commit hash."""
    found: dict[str, dict[str, Any]] = {}
    for candidate in service.store.list_candidates():
        head = str(candidate.get("head_commit") or "")
        if head not in hashes:
            continue
        job = service.store.latest_job_for_commit(head)
        if job is None:
            continue
        found[head] = {
            "candidate": int(candidate["id"]),
            "node": candidate.get("node"),
            "status": job.get("status"),
            "duration": job.get("duration"),
            "fingerprint": job.get("fingerprint"),
            "source": job.get("source"),
            "commands": job.get("commands"),
            "output": job.get("output"),
            "gpu": job.get("gpu"),
            "created_at": job.get("finished_at") or job.get("requested_at"),
        }
    return found


def file_lines(
    service: "Service", commit: str | None, path: str
) -> dict[str, Any]:
    """The parsed lines for one file, loaded on demand by ``/api/diff``."""
    source_tip = _rev(service, _worktree_branch(service))
    target_tip = _rev(service, _target_branch(service))
    base, head = target_tip, source_tip
    if commit:
        parent = _parent(service, commit)
        if parent:
            base = parent
        head = commit
    lines = diff.file_diff(service.root, base, head, path) if base and head else []
    return {"commit": commit, "file": path, "lines": lines}


def _safe_path(path: str) -> str:
    """Reject a path that can escape the repository or confuse ``git show``."""
    if not path or path.startswith("-") or "\x00" in path:
        raise SlicemeError("invalid file path")
    posix = PurePosixPath(path)
    if posix.is_absolute() or ".." in posix.parts or ":" in path:
        raise SlicemeError("invalid file path")
    return path


def file_content(
    service: "Service", commit: str | None, path: str
) -> dict[str, Any]:
    """The full text of one file at one commit, for a Markdown preview.

    The client reads the whole file, not the diff, so the preview shows the
    committed Markdown.  The path is validated first, then read from git.  A
    binary file is refused.
    """
    safe = _safe_path(path)
    ref = (
        commit
        or _rev(service, _worktree_branch(service))
        or _rev(service, _target_branch(service))
    )
    if not ref:
        raise SlicemeError("no commit is available to read")
    try:
        res = gitutil.git(service.root, "show", f"{ref}:{safe}", check=False)
    except UnicodeDecodeError:
        raise SlicemeError(f"{safe} is not a text file") from None
    if not res.ok:
        raise SlicemeError(f"cannot read {safe} at {commit or ref}")
    if "\x00" in res.stdout:
        raise SlicemeError(f"{safe} is not a text file")
    return {"commit": commit, "file": safe, "ref": ref, "content": res.stdout}
