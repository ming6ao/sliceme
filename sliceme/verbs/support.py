"""Shared internal helpers for the verb-group mixins.

:func:`campaign_lock` and :func:`_delivery_lock` serialize git mutation; the
rest are small pure helpers used by more than one verb group.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .. import campaign, gitutil
from ..ownership import path_within_owns
from ..store import Store
from ..util import SlicemeError, slugify, state_dir


def _subject_line(text: str | None) -> str:
    """Return the first non-empty, whitespace-collapsed line of *text*."""
    for line in str(text or "").splitlines():
        collapsed = " ".join(line.split())
        if collapsed:
            return collapsed
    return ""


@contextmanager
def campaign_lock(root: Path):
    """The campaign lock: serializes campaign creation and wave recording.

    Checks are synchronous and run inside the caller, so they need no lock of
    their own; this lock guards the git mutation of a wave record.
    """
    import fcntl

    from ..util import state_dir

    path = state_dir(root) / "campaigns.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _delivery_lock(root: Path):
    """The plane delivery lock, separate from the campaign lock (POSIX only)."""
    import fcntl

    from ..util import state_dir

    path = state_dir(root) / "review.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _campaign_has_work(store: Store, root: Path, campaign_row: dict[str, Any]) -> bool:
    """Whether a campaign already recorded a DAG, a candidate, or a unit."""
    if campaign.dag_path(root, str(campaign_row["target_branch"])).exists():
        return True
    if store.list_candidates(campaign=str(campaign_row["key"])):
        return True
    return store.get_unit_by_campaign(str(campaign_row["key"])) is not None


def _owners_of(path: str, owners: dict[str, list[str]]) -> list[str]:
    return [node_id for node_id, owns in owners.items() if path_within_owns(path, owns)]


def _record_error(reason: str, message: str) -> SlicemeError:
    """A record error carrying a machine-readable reason code.

    The code is on both the ``reason`` attribute and at the head of the
    message, so a caller can switch on the code and never parse the text.
    """
    error = SlicemeError(f"{reason}: {message}")
    error.reason = reason
    return error


def _describe_violation(
    status: str,
    path: str,
    old: str | None,
    new_owners: list[str],
    old_owners: list[str],
) -> str:
    if old:
        return f"{status} {old} -> {path} spans nodes {old_owners}/{new_owners}"
    if not new_owners:
        return f"{status} {path} is outside every wave node"
    return f"{status} {path} is claimed by {new_owners}"


def _changed_entries(
    worktree: Path, base: str | None = None
) -> list[tuple[str, str, str | None]]:
    """Staged changes as ``(status, path, old_path)``, rename-aware.

    With no *base* the diff is against ``HEAD`` (the last recorded wave).
    """
    args = ["diff", "--cached", "--name-status", "-M"]
    if base:
        args.append(base)
    result = gitutil.git(worktree, *args, check=False)
    if not result.ok:
        return []
    entries: list[tuple[str, str, str | None]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status[:1] in {"R", "C"} and len(parts) >= 3:
            entries.append((status, parts[2], parts[1]))
        else:
            entries.append((status, parts[1], None))
    return entries


def _resolve_target_branch(
    root: Path,
    *,
    target_branch: str | None,
    target_mode: str | None,
    base: str | None,
) -> str:
    """Resolve the campaign target branch from the user's choice.

    ``current`` adopts the checked-out branch; ``existing`` requires the named
    branch to exist; ``new`` creates it from *base* (default ``HEAD``).  A bare
    target name with no mode is treated as an existing branch.
    """
    mode = (target_mode or "").strip().lower()
    if mode not in {"", "current", "existing", "new"}:
        raise SlicemeError("target_mode must be one of: current, existing, new")
    if mode == "new":
        if not target_branch:
            raise SlicemeError("target_mode 'new' requires a target branch name")
        if gitutil.branch_exists(root, target_branch):
            raise SlicemeError(f"branch '{target_branch}' already exists")
        from_commit = base or gitutil.rev_parse(root, "HEAD")
        gitutil.create_branch(root, target_branch, from_commit)
        return target_branch
    if target_branch:
        if not gitutil.branch_exists(root, target_branch):
            raise SlicemeError(f"branch '{target_branch}' does not exist")
        return target_branch
    current = gitutil.current_branch(root)
    if not current:
        raise SlicemeError(
            "not on a branch; create or check out the campaign branch before start"
        )
    return current


def _default_worktree_branch(root: Path, target: str) -> str:
    """A stable, unique accumulation branch for the campaign worktree."""
    base = f"sliceme/{slugify(target or 'campaign', 32)}"
    branch = base
    counter = 2
    while gitutil.branch_exists(root, branch):
        branch = f"{base}-{counter}"
        counter += 1
    return branch


def _unique_branch(root: Path, name: str) -> str:
    base = f"sliceme/{slugify(name, 32)}"
    branch = base
    counter = 2
    while gitutil.branch_exists(root, branch):
        branch = f"{base}-{counter}"
        counter += 1
    return branch


def _unique_worktree(base_dir: Path, name: str) -> Path:
    candidate = base_dir / slugify(name, 32)
    path = candidate
    counter = 2
    while path.exists():
        path = candidate.with_name(candidate.name + f"-{counter}")
        counter += 1
    return path


def _ensure_gitignore(root: Path) -> None:
    """Ignore ``.sliceme/`` via the repo-local exclude file.

    Using ``.git/info/exclude`` (shared by all worktrees) keeps the main
    worktree clean, unlike creating an untracked ``.gitignore``.
    """
    entry = ".sliceme/"
    res = gitutil.git(root, "rev-parse", "--git-common-dir", check=False)
    git_dir = Path(res.stdout.strip()) if res.ok else root / ".git"
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    content = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if entry in {line.strip() for line in content.splitlines()}:
        return
    if content and not content.endswith("\n"):
        content += "\n"
    exclude.write_text(content + entry + "\n", encoding="utf-8")

