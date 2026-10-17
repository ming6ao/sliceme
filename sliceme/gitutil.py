"""Git plumbing.

Mutating operations shell out to the system ``git`` (worktree, merge, rebase,
merge-tree, update-ref) as prescribed by ``docs/reference.md``.  The
module is deliberately small and side-effect free apart from the named verbs.
"""

from __future__ import annotations

import os
import re
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .util import SlicemeError, rmtree, state_dir


@dataclass
class GitResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def git(
    repo: str | os.PathLike[str],
    *args: str,
    check: bool = False,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> GitResult:
    cmd = ["git", "-C", str(repo), *args]
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        env=full_env,
    )
    result = GitResult(list(args), proc.returncode, proc.stdout, proc.stderr)
    if check and not result.ok:
        raise SlicemeError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def is_git_repo(path: str | os.PathLike[str]) -> bool:
    return git(path, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def toplevel(path: str | os.PathLike[str]) -> Path:
    res = git(path, "rev-parse", "--show-toplevel", check=True)
    return Path(res.stdout.strip()).resolve()


def rev_parse(repo: str | os.PathLike[str], ref: str) -> str:
    res = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if not res.ok:
        res = git(repo, "rev-parse", "--verify", ref, check=False)
    if not res.ok:
        raise SlicemeError(f"cannot resolve git ref: {ref}")
    return res.stdout.strip()


def branch_exists(repo: str | os.PathLike[str], branch: str) -> bool:
    return git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").ok


def tree_of(repo: str | os.PathLike[str], commit: str) -> str:
    res = git(repo, "rev-parse", f"{commit}^{{tree}}", check=True)
    return res.stdout.strip()


def status_porcelain(worktree: str | os.PathLike[str]) -> list[str]:
    res = git(worktree, "status", "--porcelain", check=True)
    return [line for line in res.stdout.splitlines() if line.strip()]


def is_clean(worktree: str | os.PathLike[str]) -> bool:
    return not status_porcelain(worktree)


def head_commit(worktree: str | os.PathLike[str]) -> str:
    return git(worktree, "rev-parse", "HEAD", check=True).stdout.strip()


@dataclass
class WorktreeEntry:
    path: Path
    head: str
    branch: str
    detached: bool


def list_worktrees(repo: str | os.PathLike[str]) -> list[WorktreeEntry]:
    res = git(repo, "worktree", "list", "--porcelain", check=True)
    entries: list[WorktreeEntry] = []
    current: dict[str, str] = {}
    for raw in res.stdout.splitlines():
        line = raw.strip()
        if not line:
            if current:
                entries.append(_entry(current))
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree ") :]
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD ") :]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch ") :].removeprefix("refs/heads/")
        elif line == "detached":
            current["detached"] = "1"
    if current:
        entries.append(_entry(current))
    return entries


def _entry(data: dict[str, str]) -> WorktreeEntry:
    return WorktreeEntry(
        path=Path(data.get("path", "")),
        head=data.get("head", ""),
        branch=data.get("branch", ""),
        detached="detached" in data,
    )


def worktree_for_branch(repo: str | os.PathLike[str], branch: str) -> WorktreeEntry | None:
    for entry in list_worktrees(repo):
        if entry.branch == branch:
            return entry
    return None


def add_worktree(
    repo: str | os.PathLike[str],
    path: Path,
    *,
    branch: str,
    base: str,
    new_branch: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if new_branch:
        git(repo, "worktree", "add", "-b", branch, str(path), base, check=True)
    else:
        git(repo, "worktree", "add", str(path), branch, check=True)


def add_detached_worktree(repo: str | os.PathLike[str], path: Path, commit: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--detach", str(path), commit, check=True)


def fetch(repo: str | os.PathLike[str], remote: str, ref: str) -> GitResult:
    """Fetch *ref* from *remote*; the caller handles a failure."""
    return git(repo, "fetch", "--quiet", remote, ref, check=False)


def push(
    repo: str | os.PathLike[str],
    remote: str,
    branch: str,
    *,
    set_upstream: bool = True,
) -> GitResult:
    """Push *branch* to *remote* for pull request delivery."""
    args = ["push"]
    if set_upstream:
        args.append("--set-upstream")
    args += [remote, branch]
    return git(repo, *args, check=True)


def remove_worktree(repo: str | os.PathLike[str], path: Path, *, force: bool = True) -> None:
    path = Path(path)
    if path.exists():
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        if git(repo, *args, check=False).ok:
            return
    # Either the directory is already gone (deleted by hand or by an
    # interrupted run) or ``git worktree remove`` could not handle it.  Without
    # a prune, the stale administrative entry keeps the branch marked as
    # "used by worktree" and blocks ``git branch -D``.
    prune_worktrees(repo)


def prune_worktrees(repo: str | os.PathLike[str]) -> None:
    git(repo, "worktree", "prune", check=False)


def delete_branch(repo: str | os.PathLike[str], branch: str, *, force: bool = True) -> None:
    flag = "-D" if force else "-d"
    git(repo, "branch", flag, branch, check=False)


def cleanup_worktree(
    repo: str | os.PathLike[str],
    path: Path,
    *,
    branch: str | None = None,
    force: bool = True,
) -> None:
    """Tear down a worktree, its directory, and (optionally) its branch.

    Safe to call when the worktree directory has already been deleted: the
    stale metadata is pruned before the branch is dropped, so
    ``git branch -D`` never fails with "used by worktree".
    """
    path = Path(path)
    remove_worktree(repo, path, force=force)
    rmtree(path)
    # ``remove_worktree`` prunes on failure, but a directory removed here could
    # still have left metadata behind; prune once more before touching the ref.
    prune_worktrees(repo)
    if branch:
        delete_branch(repo, branch, force=force)


@dataclass
class MergeOutcome:
    clean: bool
    tree: str | None
    conflicts: list[str]
    raw: str


def merge_tree(
    repo: str | os.PathLike[str], lhs: str, rhs: str
) -> MergeOutcome:
    """Test-merging two commits/branches without touching a worktree."""
    res = git(repo, "merge-tree", "--write-tree", lhs, rhs, check=False)
    if res.returncode == 0:
        tree = res.stdout.splitlines()[0].strip() if res.stdout.strip() else None
        return MergeOutcome(True, tree, [], res.stdout)
    conflicts = _parse_merge_tree_conflicts(res.stdout)
    return MergeOutcome(False, None, conflicts, res.stdout + res.stderr)


_CONFLICT_RE = re.compile(r"^CONFLICT \(([^)]+)\): (.*)$")


def _parse_merge_tree_conflicts(output: str) -> list[str]:
    found: list[str] = []
    for line in output.splitlines():
        m = _CONFLICT_RE.match(line.strip())
        if m:
            found.append(f"{m.group(1)}: {m.group(2)}")
    return found


def merge_base(repo: str | os.PathLike[str], a: str, b: str) -> str:
    res = git(repo, "merge-base", a, b, check=False)
    return res.stdout.strip()


def detect_toolchain_files(repo: str | os.PathLike[str], commit: str) -> dict[str, str]:
    """Hash lockfiles that pin the toolchain, so verification caches invalidate."""
    candidates = [
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
        "go.sum",
        "poetry.lock",
        "requirements.txt",
        "Gemfile.lock",
        "composer.lock",
    ]
    digests: dict[str, str] = {}
    for name in candidates:
        res = git(repo, "show", f"{commit}:{name}", check=False)
        if res.ok:
            from .util import sha256_text

            digests[name] = sha256_text(res.stdout)
    return digests


@contextmanager
def workspace_lock(root: Path) -> Iterator[None]:
    """The one workspace lock: serializes campaign creation, record, and delivery.

    Checks are synchronous and run inside the caller, so they need no lock of
    their own; one lock guards every git mutation in a workspace.
    """
    import fcntl

    path = state_dir(root) / "workspace.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _record_error(reason: str, message: str) -> SlicemeError:
    """A record error carrying a machine-readable reason code.

    The code is on both the ``reason`` attribute and at the head of the
    message, so a caller can switch on the code and never parse the text.
    """
    error = SlicemeError(f"{reason}: {message}")
    error.reason = reason
    return error


def _changed_entries(
    worktree: Path, base: str | None = None
) -> list[tuple[str, str, str | None]]:
    """Staged changes as ``(status, path, old_path)``, rename-aware.

    With no *base* the diff is against ``HEAD`` (the last recorded phase).
    """
    args = ["diff", "--cached", "--name-status", "-M"]
    if base:
        args.append(base)
    result = git(worktree, *args, check=False)
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
