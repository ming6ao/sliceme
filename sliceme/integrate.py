"""Agent-callable integration onto a campaign's feature branch.

``integrate`` lands prepared candidates on the plane's ``main_branch`` (the
campaign feature branch):

* candidates are ordered by the existing wave planner, then merged with
  ``git merge --no-ff`` (one merge commit per unit, branches kept);
* the plane's trusted checks run on the combined tree, fingerprint-cached;
* candidates move to ``landed`` and units to ``landed``, keeping branches for
  provenance;
* a merge conflict aborts the merge and returns structured findings, never
  leaving the feature branch half-merged;
* re-running is a no-op: landed candidates are skipped, and a candidate whose
  branch is already contained in the feature branch is marked landed.

A **safety rail** refuses to integrate when ``main_branch`` equals the plane's
recorded default branch (captured once at init, §6.1).  Promotion to the
default branch stays a human ``git`` step.

When ``check_only`` is set (the orchestrator's ``verify`` step), the node's
acceptance commands run at the candidate commit, the verdict is recorded with
source ``node:<id>``, and nothing is merged.

This module also owns candidate integration ordering and simulation: prepared
candidates are grouped into the campaign DAG's wave order, and ``simulate``
materializes each wave's combined tree so the plane's trusted checks can run
once over the combined result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import campaign, gitutil
from .ownership import DEFAULT_WAVE_SIZE, plan_dag_waves
from .store import Store
from .util import SlicemeError, scratch_dir, worktrees_dir
from .verifier import CheckResult, run_checks

__all__ = [
    "LandResult",
    "Wave",
    "deliver",
    "found_default_branch",
    "is_default_branch",
    "plan_waves",
    "simulate",
    "target_branch_of",
]


# ---------------------------------------------------------------------------
# Integration primitives
# ---------------------------------------------------------------------------
@dataclass
class LandResult:
    candidate_id: int
    unit_name: str
    branch: str
    status: str  # landed | failed | skipped
    detail: str = ""
    merge_commit: str | None = None
    checks: list[CheckResult] = field(default_factory=list)
    already_up_to_date: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate_id,
            "unit": self.unit_name,
            "branch": self.branch,
            "status": self.status,
            "detail": self.detail,
            "merge_commit": self.merge_commit,
            "already_up_to_date": self.already_up_to_date,
            "checks": [c.to_dict() for c in self.checks],
        }


#: Conventional names that can never be a campaign target branch.
DEFAULT_BRANCH_NAMES = ("main", "master")


def target_branch_of(config: dict[str, Any]) -> str:
    """The campaign's target (feature) branch: where delivery finally lands."""
    return (
        config.get("target_branch")
        or config.get("main_branch")
        or "main"
    )


# Backwards-compatible name (the target branch used to be called main_branch).
main_branch_of = target_branch_of


def main_worktree(root: Path, branch: str) -> tuple[Path, bool]:
    """Return the worktree checked out at *branch*, creating ``_integration``.

    The second element is ``True`` when the worktree was just created.
    """
    entry = gitutil.worktree_for_branch(root, branch)
    if entry is not None:
        return entry.path, False
    path = worktrees_dir(root) / "_integration"
    # Clear any leftover directory *and* stale metadata before (re)creating it,
    # otherwise ``git worktree add`` fails with "already registered".
    gitutil.cleanup_worktree(root, path)
    gitutil.add_worktree(root, path, branch=branch, base=branch, new_branch=False)
    return path, True


def conflict_summary(result: gitutil.GitResult) -> str:
    text = (result.stderr or "") + "\n" + (result.stdout or "")
    for line in text.splitlines():
        line = line.strip()
        if (
            line.startswith("CONFLICT")
            or "Automatic merge failed" in line
            or "would be overwritten" in line
        ):
            return line
    return text.strip().splitlines()[-1] if text.strip() else "merge failed"


def found_default_branch(root: Path, *, exclude: str | None = None) -> str:
    """Derive the repository's default branch.

    Used as a fallback for planes created before the value was recorded.
    Precedence: ``refs/remotes/origin/HEAD``, ``init.defaultBranch``, the first
    existing branch among ``main``/``master`` (skipping *exclude*), then
    ``main``.

    The checked-out branch is deliberately **not** a fallback: ``start`` adopts
    the current branch as the campaign feature branch, so at plane-init time the
    current branch is the feature branch, never the default.
    """
    origin = gitutil.git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if origin.ok and origin.stdout.strip():
        return origin.stdout.strip().removeprefix("origin/")
    configured = gitutil.git(root, "config", "--get", "init.defaultBranch", check=False)
    if configured.ok and configured.stdout.strip():
        name = configured.stdout.strip()
        if name != exclude:
            return name
    for candidate in ("main", "master"):
        if candidate != exclude and gitutil.branch_exists(root, candidate):
            return candidate
    return "main"


def is_default_branch(
    root: Path, name: str | None, config: dict[str, Any] | None = None
) -> bool:
    """Whether *name* is a branch sliceme must never commit to.

    ``main`` and ``master`` are reserved by convention, and the repository's
    recorded default branch is reserved outright.  There is deliberately no
    override: delivery always lands on a feature branch.
    """
    if not name:
        return False
    if name in DEFAULT_BRANCH_NAMES:
        return True
    recorded = (config or {}).get("default_branch")
    if recorded and name == recorded:
        return True
    return name == found_default_branch(root)


# ---------------------------------------------------------------------------
# Final delivery: merge the campaign worktree into the target branch
# ---------------------------------------------------------------------------
def _mark_delivered(store: Store) -> None:
    """Mark every prepared candidate as landed without rewriting its commit.

    The candidate's ``head_commit`` is provenance (the node's commit on the
    campaign worktree), so delivery must not overwrite it with the merge commit.
    """
    for candidate in store.list_candidates(statuses=["prepared"]):
        store.update_candidate(int(candidate["id"]), status="landed")
        unit = store.get_unit(int(candidate["unit_id"]))
        if unit:
            store.set_unit_state(int(unit["id"]), "landed")
    store.conn.commit()


def _deliver_branch(
    store: Store,
    root: Path,
    config: dict[str, Any],
    *,
    target: str,
    source: str,
    no_ff: bool,
    run_checks_flag: bool,
) -> list[LandResult]:
    """Merge a single campaign worktree branch onto the target branch."""
    wt_path, _created = main_worktree(root, target)
    if not gitutil.is_clean(wt_path):
        raise SlicemeError(
            f"integration worktree {wt_path} is dirty; commit or discard changes before deliver"
        )
    source_head = gitutil.rev_parse(root, source)
    target_head = gitutil.head_commit(wt_path)
    if gitutil.merge_base(root, target_head, source_head) == source_head:
        _mark_delivered(store)
        return [
            LandResult(
                candidate_id=0,
                unit_name="campaign",
                branch=source,
                status="landed",
                detail=f"already contained in {target}",
                merge_commit=target_head,
                already_up_to_date=True,
            )
        ]

    pre_merge = target_head
    merge = gitutil.merge_into(
        wt_path,
        source,
        message=f"sliceme deliver {source}",
        no_ff=no_ff,
    )
    if not merge.ok:
        gitutil.merge_abort(wt_path)
        return [
            LandResult(
                candidate_id=0,
                unit_name="campaign",
                branch=source,
                status="failed",
                detail="merge conflict: " + conflict_summary(merge),
            )
        ]

    merge_commit = gitutil.head_commit(wt_path)
    checks: list[CheckResult] = []
    if run_checks_flag:
        status, checks, _duration = run_checks(root, config, merge_commit)
        if status != "passed":
            gitutil.reset_hard(wt_path, pre_merge)
            return [
                LandResult(
                    candidate_id=0,
                    unit_name="campaign",
                    branch=source,
                    status="failed",
                    detail="combined checks failed; target branch restored",
                    checks=checks,
                )
            ]

    _mark_delivered(store)
    return [
        LandResult(
            candidate_id=0,
            unit_name="campaign",
            branch=source,
            status="landed",
            detail=f"merged into {target}",
            merge_commit=merge_commit,
            checks=checks,
        )
    ]


def deliver(
    store: Store,
    root: Path,
    config: dict[str, Any],
    *,
    target: str | None = None,
    source: str | None = None,
    no_ff: bool = True,
    run_checks_flag: bool = True,
) -> list[LandResult]:
    """Merge the campaign worktree into the target feature branch.

    Called once, after every wave is recorded and every commit is approved.
    The target branch is never the default branch: there is no override.
    """
    target = target or target_branch_of(config)
    if is_default_branch(root, target, config):
        raise SlicemeError(
            f"refusing to merge into the default branch '{target}'; "
            "sliceme never commits to main or master"
        )
    source = source or config.get("worktree_branch")
    if not source or not gitutil.branch_exists(root, source):
        raise SlicemeError(
            "no campaign worktree branch to deliver; run `sliceme wave --open` first"
        )
    return _deliver_branch(
        store,
        root,
        config,
        target=target,
        source=str(source),
        no_ff=no_ff,
        run_checks_flag=run_checks_flag,
    )


# ---------------------------------------------------------------------------
# Candidate ordering and combined-tree simulation
# ---------------------------------------------------------------------------
@dataclass
class Wave:
    index: int
    candidates: list[dict[str, Any]] = field(default_factory=list)
    combined: str = ""
    check_status: str | None = None
    checks: list[CheckResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wave": self.index,
            "combined": self.combined,
            "members": [
                {"candidate": c["id"], "unit": c["unit_name"], "branch": c["unit_branch"]}
                for c in self.candidates
            ],
            "check_status": self.check_status,
            "checks": [c.to_dict() for c in self.checks],
        }


def _wave_index_by_node(root: Path, config: dict[str, Any]) -> dict[str, int]:
    """Map every DAG node id to its wave index (empty for a non-campaign plane)."""
    branch = config.get("main_branch")
    dag = campaign.load_dag(root, branch) if branch else None
    if not dag or not dag.get("nodes"):
        return {}
    wave_size = int(dag.get("concurrency") or DEFAULT_WAVE_SIZE)
    planned = plan_dag_waves(list(dag["nodes"]), wave_size=wave_size)
    return {member: w.index for w in planned for member in w.members}


def plan_waves(
    store: Store,
    root: Path,
    config: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[Wave]:
    """Group candidates into DAG waves, each internally priority-ordered."""
    if not candidates:
        return []
    wave_of = _wave_index_by_node(root, config)
    fallback = (max(wave_of.values()) + 1) if wave_of else 0

    def key(candidate: dict[str, Any]) -> tuple[Any, ...]:
        node = str(candidate.get("node") or candidate.get("unit_name") or "")
        return (
            wave_of.get(node, fallback),
            -int(candidate.get("priority") or 0),
            float(candidate.get("created_at") or 0.0),
            int(candidate["id"]),
        )

    ordered = sorted(candidates, key=key)
    waves: list[Wave] = []
    by_index: dict[int, Wave] = {}
    for candidate in ordered:
        node = str(candidate.get("node") or candidate.get("unit_name") or "")
        index = wave_of.get(node, fallback)
        wave = by_index.get(index)
        if wave is None:
            wave = Wave(index=index)
            by_index[index] = wave
            waves.append(wave)
        wave.candidates.append(candidate)
    waves.sort(key=lambda w: w.index)
    return waves


def _synthetic_commit(root: Path, tree: str, parents: list[str], message: str) -> str:
    args = ["commit-tree", tree]
    for parent in parents:
        args += ["-p", parent]
    args += ["-m", message]
    return gitutil.git(root, *args, check=True).stdout.strip()


def _merge_into_wave(root: Path, combined: str, branch: str) -> tuple[bool, str]:
    outcome = gitutil.merge_tree(root, combined, branch)
    if not outcome.clean or not outcome.tree:
        return False, combined
    new_ref = _synthetic_commit(
        root, outcome.tree, [combined, gitutil.rev_parse(root, branch)], "sliceme wave combine"
    )
    return True, new_ref


def simulate(
    store: Store,
    root: Path,
    config: dict[str, Any],
    *,
    statuses: list[str] | None = None,
    run_checks_flag: bool = True,
) -> dict[str, Any]:
    candidates = store.list_candidates(statuses=statuses or ["prepared", "pending"])
    waves = plan_waves(store, root, config, candidates)
    base_ref = config.get("base") or config.get("main_branch") or "main"
    scratch = scratch_dir(root)
    for wave in waves:
        combined = gitutil.rev_parse(root, base_ref)
        for candidate in wave.candidates:
            ok, combined = _merge_into_wave(root, combined, candidate["unit_branch"])
            if not ok:
                combined = ""
                break
        wave.combined = combined
        if not run_checks_flag or not wave.combined:
            continue
        path = scratch / f"wave-{wave.index}-{abs(hash(wave.combined)) % 10_000_000}"
        try:
            gitutil.add_detached_worktree(root, path, wave.combined)
            status, results, _duration = run_checks(root, config, wave.combined, worktree=path)
            wave.check_status = status
            wave.checks = results
        except SlicemeError:
            wave.check_status = "error"
        finally:
            gitutil.cleanup_worktree(root, path)
    return {
        "candidate_count": len(candidates),
        "waves": [w.to_dict() for w in waves],
        "overall": _overall_status(waves),
    }


def _overall_status(waves: list[Wave]) -> str:
    for wave in waves:
        if wave.check_status not in (None, "passed"):
            return "failed"
    return "pass"

