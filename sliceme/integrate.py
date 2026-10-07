"""Agent-callable integration onto a campaign's feature branch.

``integrate`` opens the delivery pull request for a campaign:

* every approved commit accumulates on one campaign worktree branch;
* delivery pushes that branch and opens one pull request against the target
  feature branch with the ``gh`` program;
* a ``git merge-tree`` pre-check refuses a conflicting merge before the push;
* the plane's trusted checks run on the campaign head before the push;
* candidates move to ``landed`` and units to ``landed``, keeping branches for
  provenance;
* re-running is a no-op: an open pull request is returned as is.

A **safety rail** refuses delivery when the target equals the plane's recorded
(default) branch (captured once at init, §6.1).  Promotion to the default
branch stays a human act on the forge.

When ``check_only`` is set (the orchestrator's ``verify`` step), the node's
acceptance commands run at the candidate commit, the verdict is recorded with
source ``node:<id>``, and nothing is merged.

This module also owns candidate integration ordering and simulation: prepared
candidates are grouped into the campaign DAG's wave order, and ``simulate``
materializes each wave's combined tree so the plane's trusted checks can run
once over the combined result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import campaign, gitutil, pullrequest
from .ownership import DEFAULT_WAVE_SIZE, plan_dag_waves
from .store import Store
from .util import SlicemeError, scratch_dir
from .verifier import CheckResult, checks_from_config, run_checks

__all__ = [
    "LandResult",
    "Wave",
    "deliver_pull_request",
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
    status: str  # landed | failed
    detail: str = ""
    pull_request: dict[str, Any] | None = None
    checks: list[CheckResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "pull_request": self.pull_request,
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
# Final delivery: push the campaign branch and open a pull request
# ---------------------------------------------------------------------------
def _mark_delivered(store: Store, campaign: str | None = None) -> None:
    """Mark this campaign's prepared candidates as landed.

    The candidate's ``head_commit`` stays as provenance (the node's commit on
    the campaign worktree).
    """
    for candidate in store.list_candidates(statuses=["prepared"], campaign=campaign):
        store.update_candidate(int(candidate["id"]), status="landed")
        unit = store.get_unit(int(candidate["unit_id"]))
        if unit:
            store.set_unit_state(int(unit["id"]), "landed")
    store.conn.commit()


def landed(
    detail: str,
    pull_request: dict[str, Any] | None = None,
    *,
    checks: list[CheckResult] | None = None,
) -> LandResult:
    return LandResult(
        status="landed",
        detail=detail,
        pull_request=pull_request,
        checks=checks or [],
    )


def _conflict_detail(outcome: gitutil.MergeOutcome) -> str:
    if outcome.conflicts:
        return "merge conflict: " + "; ".join(outcome.conflicts[:5])
    text = (outcome.output or "").strip()
    last = text.splitlines()[-1] if text else "merge failed"
    return "merge conflict: " + last


def _check_results(job: dict[str, Any]) -> list[CheckResult]:
    """Rebuild the per-check evidence the executor persisted on *job*."""
    raw = job.get("results")
    if not raw:
        return []
    return [CheckResult(**item) for item in json.loads(raw)]


def _delivery_executor(
    store: Store, root: Path, config: dict[str, Any], campaign_key: str | None
):
    """The single executor, for the plane's trusted checks before a push."""
    from .executor import Executor

    branch = config.get("main_branch")
    dag = campaign.load_dag(root, branch) if branch else None
    return Executor(root, store, config, dag=dag, campaign=campaign_key)


def deliver_pull_request(
    store: Store,
    root: Path,
    config: dict[str, Any],
    *,
    campaign: str | None = None,
    target: str | None = None,
    source: str | None = None,
    run_checks_flag: bool = True,
) -> list[LandResult]:
    """Push the campaign worktree branch and open the delivery pull request.

    Called once, after every wave is recorded and every commit is approved.
    The target branch is never the default branch: there is no override.
    """
    target = target or target_branch_of(config)
    if is_default_branch(root, target, config):
        raise SlicemeError(
            f"refusing to deliver onto the default branch '{target}'; "
            "sliceme opens a pull request against a feature branch only"
        )
    source = source or config.get("worktree_branch")
    if not source or not gitutil.branch_exists(root, source):
        raise SlicemeError(
            "no campaign worktree branch to deliver; run `sliceme wave --open` first"
        )
    source = str(source)
    if not gitutil.branch_exists(root, target):
        raise SlicemeError(f"target branch '{target}' does not exist")

    # Fail before the push (and the checks) when the forge client is absent, so
    # a missing program never leaves a pushed branch without a pull request.
    pullrequest.require()

    source_head = gitutil.rev_parse(root, source)
    target_head = gitutil.rev_parse(root, target)
    if gitutil.merge_base(root, target_head, source_head) == source_head:
        _mark_delivered(store, campaign)
        return [landed(f"already contained in {target}")]

    outcome = gitutil.merge_tree(root, target_head, source_head)
    if not outcome.clean:
        return [
            LandResult(
                status="failed",
                detail=_conflict_detail(outcome),
            )
        ]

    # The trusted checks run through the single executor, not beside it: a
    # submit whose fingerprint already reached a terminal verdict is honored
    # as cached (DEC-3), and the single drain serializes the run.
    checks: list[CheckResult] = []
    if run_checks_flag:
        specs = checks_from_config(config)
        if specs:
            executor = _delivery_executor(store, root, config, campaign)
            submitted = executor.submit(
                source="deliver",
                commit=source_head,
                checks=specs,
                priority=1,
            )
            if not submitted["cached"]:
                executor.drain()
            job = executor.wait(submitted["job"]["id"], timeout=3600.0)
            checks = _check_results(job)
            if job["status"] != "passed":
                return [
                    LandResult(
                        status="failed",
                        detail="checks failed; pull request not opened",
                        checks=checks,
                    )
                ]

    remote = (config.get("policy") or {}).get("remote") or "origin"
    gitutil.push(root, remote, source)

    found = pullrequest.find(root, source)
    if found is None:
        # Local import avoids the parameter named ``campaign`` shadowing the module.
        from .campaign import pull_request_content

        title, body = pull_request_content(
            root, config, store, campaign=campaign
        )
        found = pullrequest.create(
            root, head=source, base=target, title=title, body=body
        )

    _mark_delivered(store, campaign)
    return [
        landed(
            f"pull request opened: {found['url']}",
            found,
            checks=checks,
        )
    ]


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
    campaign: str | None = None,
    statuses: list[str] | None = None,
    run_checks_flag: bool = True,
) -> dict[str, Any]:
    candidates = store.list_candidates(
        statuses=statuses or ["prepared", "pending"], campaign=campaign
    )
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

