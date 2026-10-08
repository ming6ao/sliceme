"""Build one review snapshot: the accumulated commits and the report.

The review unit is the campaign worktree.  Commits accumulate as waves land, so
a human can review and approve them before or after the campaign finishes.  One
campaign-level approval covers the whole commit set; delivery proceeds only
while the newest campaign decision is an unconsumed ``approve``.

The report (``.sliceme/<branch-key>.report.md``) is git-ignored, so it is read
from disk and included in the packet as a virtual file.  It is evidence for the
reviewer, not a merge gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import campaign, gitutil, integrate
from ..util import SlicemeError, now
from . import diff

if TYPE_CHECKING:  # pragma: no cover
    from ..service import Service

__all__ = [
    "MARKDOWN_OUTPUT_LIMIT",
    "bound_output",
    "build_packet",
    "campaign_branch_key",
    "campaign_commits",
    "commit_changes",
    "commit_hashes",
    "diffstat",
    "report_info",
    "review_commits",
]

#: The largest check-output text that the Markdown evidence document keeps.
#: The JSON evidence keeps the full output, so a reviewer can read all of it.
MARKDOWN_OUTPUT_LIMIT = 2000


def bound_output(output: str | None, limit: int = MARKDOWN_OUTPUT_LIMIT) -> str:
    """Truncate one check output for the Markdown evidence document.

    The JSON evidence keeps the full output.  The Markdown keeps *limit*
    characters and states how many characters it omitted, so the document
    stays a manageable size.
    """
    text = str(output or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} characters omitted]"


def diffstat(files: list[dict[str, Any]]) -> dict[str, int]:
    """The one-commit summary: the file count, the additions, and the deletions."""
    return {
        "files": len(files),
        "additions": sum(int(row.get("additions") or 0) for row in files),
        "deletions": sum(int(row.get("deletions") or 0) for row in files),
    }


def _parent_of(root: Path, commit: str) -> str | None:
    res = gitutil.git(root, "rev-parse", f"{commit}^", check=False)
    return res.stdout.strip() if res.ok else None


def commit_changes(root: Path, commit: str) -> dict[str, Any]:
    """The changed files and the diffstat for one commit.

    The change set is the commit against its first parent, so an evidence row
    names what that one commit changed.  A root commit has no parent, so its
    change set is empty.
    """
    parent = _parent_of(root, commit)
    files = diff.file_index(root, parent, commit) if parent else []
    return {"diffstat": diffstat(files), "files": files}


def campaign_branch_key(service: "Service") -> str:
    config = service.config
    branch = str(config.get("target_branch") or config.get("main_branch") or "main")
    return campaign.branch_key(branch)


def _campaign_branch(service: "Service") -> str:
    config = service.config
    return str(config.get("target_branch") or config.get("main_branch") or "main")


def _target_branch(service: "Service") -> str:
    """The review diff base: the delivery base (the pull request base).

    ``_campaign_branch`` is the pull request head.  Diffing the head against
    itself gives an empty campaign, so the diff base is the delivery base
    instead.
    """
    return integrate.delivery_base_of(service.config)


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
    for candidate in service.store.list_candidates(
        statuses=["prepared"], campaign=service.campaign_key()
    ):
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
    branch = _campaign_branch(service)
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


def _campaign_approval(service: "Service") -> tuple[dict[str, Any] | None, bool]:
    """The latest campaign-level decision and whether it is an approve."""
    return service.campaign_decision(), service.campaign_approved()


def build_packet(service: "Service", *, commit: str | None = None) -> dict[str, Any]:
    """One coherent review snapshot for the client to poll."""
    target = _target_branch(service)
    source_ref = _worktree_branch(service)
    source_tip = _rev(service, source_ref)
    target_tip = _rev(service, target)
    branch_key = campaign_branch_key(service)

    commits: list[dict[str, Any]] = []
    if source_tip and target_tip:
        commits = diff.commits(service.root, target_tip, source_tip)
    approval, approved = _campaign_approval(service)
    for row in commits:
        row["approved"] = approved

    base, head = target_tip, source_tip
    if commit:
        parent = _parent(service, commit)
        if parent:
            base = parent
        head = commit

    return {
        "branch_key": branch_key,
        "feature_branch": _campaign_branch(service),
        "source_tip": source_tip,
        "target_tip": target_tip,
        "commit": commit,
        "commits": commits,
        "all_approved": approved,
        "override": approval,
        "files": diff.file_index(service.root, base, head) if base and head else [],
        "evidence": _evidence_map(service, {row["hash"] for row in commits}),
        "report": report_info(service),
        "updated_at": now(),
    }


def _evidence_map(service: "Service", hashes: set[str]) -> dict[str, dict[str, Any]]:
    """The newest verification for each reviewed commit, keyed by commit hash."""
    found: dict[str, dict[str, Any]] = {}
    for candidate in service.store.list_candidates(campaign=service.campaign_key()):
        head = str(candidate.get("head_commit") or "")
        if head not in hashes:
            continue
        check = service.store.latest_check_for_commit(head)
        if check is None:
            continue
        changes = commit_changes(service.root, head)
        found[head] = {
            "candidate": int(candidate["id"]),
            "node": candidate.get("node"),
            "status": check.get("status"),
            "duration": check.get("duration"),
            "fingerprint": check.get("fingerprint"),
            "source": check.get("source"),
            "commands": check.get("commands"),
            "output": check.get("output"),
            "gpu": check.get("gpu"),
            "created_at": check.get("finished_at") or check.get("created_at"),
            "diffstat": changes["diffstat"],
            "files": changes["files"],
        }
    return found


