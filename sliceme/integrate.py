"""Agent-callable integration onto a campaign's feature branch.

``integrate`` opens the delivery pull request for a campaign:

* every approved commit accumulates on one campaign worktree branch;
* delivery pushes that branch and opens one pull request against the delivery
  base, the repository default branch, with the ``gh`` program;
* a ``git merge-tree`` pre-check refuses a conflicting merge before the push;
* the plane's trusted checks run on the campaign head before the push;
* records move to ``delivered`` and the campaign to ``delivered``, keeping
  branches for provenance;
* re-running is a no-op: an open pull request is returned as is.

A **safety rail** refuses delivery when the campaign branch equals the plane's
recorded (default) branch (captured once at init, §6.1).  The campaign branch
is the pull request head, so it must stay a feature branch.

When ``check_only`` is set (the orchestrator's ``verify`` step), the node's
acceptance commands run at the record commit, the verdict is recorded with
source ``node:<id>``, and nothing is merged.

This module also owns record integration ordering: recorded records are grouped
into the campaign DAG's phase order for the status projection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import campaign, gitutil, pullrequest
from .ownership import DEFAULT_PHASE_SIZE, plan_dag_phases
from .store import Store
from .util import SlicemeError, scratch_dir
from .verifier import CheckResult, checks_from_config

__all__ = [
    "DELIVERY_MODES",
    "DeliveryResult",
    "Phase",
    "base_branch_of",
    "deliver",
    "deliver_local_merge",
    "deliver_pull_request",
    "delivery_base_of",
    "found_default_branch",
    "is_default_branch",
    "local_result",
    "plan_phases",
    "resolve_delivery_mode",
    "target_branch_of",
    "work_branch_of",
]


# ---------------------------------------------------------------------------
# Integration primitives
# ---------------------------------------------------------------------------
#: The two delivery modes.  ``pull_request`` pushes the campaign branch and
#: opens a pull request.  ``local`` merges the campaign branch into the local
#: base branch and stops, so the user pushes the base branch later.
DELIVERY_MODES = ("pull_request", "local")


@dataclass
class DeliveryResult:
    status: str  # delivered | failed
    detail: str = ""
    pull_request: dict[str, Any] | None = None
    checks: list[CheckResult] = field(default_factory=list)
    mode: str = "pull_request"
    merge_commit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "pull_request": self.pull_request,
            "checks": [c.to_dict() for c in self.checks],
            "mode": self.mode,
            "merge_commit": self.merge_commit,
        }


#: Conventional names that can never be a campaign branch.
DEFAULT_BRANCH_NAMES = ("main", "master")


def work_branch_of(config: dict[str, Any]) -> str:
    """The campaign work branch: the branch the campaign commits on.

    The campaign work branch is the pull request head and the campaign
    identity.  It must never be the default branch; the pull request base is
    :func:`base_branch_of`.  The read-time shim accepts the retired names
    (``target_branch``, ``main_branch``, ``worktree_branch``,
    ``feature_branch``) for one release.
    """
    return (
        config.get("work_branch")
        or config.get("worktree_branch")
        or config.get("target_branch")
        or config.get("main_branch")
        or config.get("feature_branch")
        or "main"
    )


def base_branch_of(config: dict[str, Any]) -> str:
    """The pull request base: the branch the campaign lands on.

    The base branch and the protected ``default_branch`` are the same value
    only until a caller passes ``--base``.  The read-time shim accepts the
    retired names (``delivery_base``, ``base``) for one release.
    """
    return (
        config.get("base_branch")
        or config.get("delivery_base")
        or config.get("base")
        or config.get("default_branch")
        or "main"
    )


# Retired names kept as read aliases for one release.
def target_branch_of(config: dict[str, Any]) -> str:
    """Deprecated alias of :func:`work_branch_of`."""
    return work_branch_of(config)


main_branch_of = work_branch_of


def delivery_base_of(config: dict[str, Any]) -> str:
    """Deprecated alias of :func:`base_branch_of`."""
    return base_branch_of(config)


def found_default_branch(root: Path, *, exclude: str | None = None) -> str:
    """Derive the repository's default branch.

    Used as a fallback for planes created before the value was recorded.
    Precedence: ``refs/remotes/origin/HEAD``, ``init.defaultBranch``, the first
    existing branch among ``main``/``master`` (skipping *exclude*), then
    ``main``.

    The checked-out branch is deliberately **not** a fallback: the campaign
    branch comes from the design name or an explicit flag, so the current
    checkout is never the default branch by accident.
    """
    origin = gitutil.git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if origin.ok and origin.stdout.strip():
        return origin.stdout.strip().removeprefix("origin/")
    configured = gitutil.git(root, "config", "--get", "init.defaultBranch", check=False)
    if configured.ok and configured.stdout.strip():
        name = configured.stdout.strip()
        if name != exclude:
            return name
    for name in ("main", "master"):
        if name != exclude and gitutil.branch_exists(root, name):
            return name
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
# Final delivery: a pull request, or a local merge
# ---------------------------------------------------------------------------
def _mark_delivered(
    store: Store, campaign: str | None = None, *, state: str = "delivered"
) -> None:
    """Mark this campaign's recorded records as delivered.

    The record's ``head_commit`` stays as provenance (the node's commit on
    the campaign worktree).  The campaign state moves to ``delivered`` for a
    pull request and to ``landed`` for a local merge, because the worktree
    holds no undelivered work after a successful delivery.
    """
    for record in store.list_records(statuses=["recorded"], campaign=campaign):
        store.update_record(int(record["id"]), status="delivered")
    if campaign:
        store.set_campaign_state(campaign, state)
    store.conn.commit()


def resolve_delivery_mode(
    config: dict[str, Any], *, mode: str | None = None
) -> str:
    """The delivery mode: the flag, then the campaign value, then the plane.

    An absent value means ``pull_request``, so a plane created by an older
    release keeps its behavior.
    """
    chosen = mode or config.get("delivery_mode") or "pull_request"
    if chosen not in DELIVERY_MODES:
        raise SlicemeError("delivery mode must be one of: " + ", ".join(DELIVERY_MODES))
    return chosen


def delivered_result(
    detail: str,
    pull_request: dict[str, Any] | None = None,
    *,
    checks: list[CheckResult] | None = None,
    status: str = "delivered",
) -> DeliveryResult:
    """A successful delivery result.

    ``status`` is ``delivered`` for a pull request that this call opened,
    ``already_contained`` when the base already holds the work, and
    ``pull_request_already_open`` for an idempotent repeat.
    """
    return DeliveryResult(
        status=status,
        detail=detail,
        pull_request=pull_request,
        checks=checks or [],
    )


def local_result(
    detail: str,
    *,
    merge_commit: str | None = None,
    checks: list[CheckResult] | None = None,
    status: str = "delivered",
) -> DeliveryResult:
    """A successful local merge result.  There is no pull request.

    ``status`` is ``delivered`` for a merge that this call applied, and
    ``already_landed`` for an idempotent repeat.
    """
    return DeliveryResult(
        status=status,
        detail=detail,
        checks=checks or [],
        mode="local",
        merge_commit=merge_commit,
    )


def already_landed_result(
    detail: str, *, merge_commit: str | None = None
) -> DeliveryResult:
    """The idempotent result for a campaign that is already landed locally."""
    return local_result(detail, merge_commit=merge_commit, status="already_landed")


def _conflict_detail(outcome: gitutil.MergeOutcome) -> str:
    if outcome.conflicts:
        return "merge conflict: " + "; ".join(outcome.conflicts[:5])
    text = (outcome.output or "").strip()
    last = text.splitlines()[-1] if text else "merge failed"
    return "merge conflict: " + last


def _check_results(row: dict[str, Any]) -> list[CheckResult]:
    """Rebuild the per-check evidence the runner persisted on *row*."""
    raw = row.get("results")
    if not raw:
        return []
    return [CheckResult(**item) for item in json.loads(raw)]


def _delivery_checks(
    store: Store, root: Path, config: dict[str, Any], campaign_key: str | None
):
    """The single check runner, for the plane's trusted checks before a push."""
    from .checks import CheckRunner

    branch = config.get("work_branch")
    dag = campaign.load_dag(root, branch) if branch else None
    return CheckRunner(root, store, config, dag=dag, campaign=campaign_key)


def deliver_pull_request(
    store: Store,
    root: Path,
    config: dict[str, Any],
    *,
    campaign: str | None = None,
    source: str | None = None,
    run_checks_flag: bool = True,
    narrative: str | None = None,
) -> list[DeliveryResult]:
    """Push the campaign worktree branch and open the delivery pull request.

    Called once, after every phase is recorded and the user confirmed the campaign.
    The pull request head is the campaign worktree branch.  The pull request
    base is the delivery base (the repository default branch).  The campaign
    branch is never the default branch: there is no override.  ``narrative`` is
    the coordinator's summary, and it leads the pull request body under the
    deterministic summary.
    """
    campaign_branch = target_branch_of(config)
    if is_default_branch(root, campaign_branch, config):
        raise SlicemeError(
            f"refusing to deliver the default branch '{campaign_branch}' as the "
            "campaign branch; sliceme pushes a feature branch only"
        )
    base = delivery_base_of(config)
    if campaign_branch == base:
        raise SlicemeError(
            f"refusing to deliver: the campaign branch '{campaign_branch}' equals "
            "the delivery base"
        )
    source = source or config.get("work_branch")
    if not source or not gitutil.branch_exists(root, source):
        raise SlicemeError(
            "no campaign worktree branch to deliver; run `sliceme open` first"
        )
    source = str(source)
    if not gitutil.branch_exists(root, base):
        raise SlicemeError(f"delivery base '{base}' does not exist")

    # Fail before the push (and the checks) when the forge client is absent, so
    # a missing program never leaves a pushed branch without a pull request.
    pullrequest.require()

    source_head = gitutil.rev_parse(root, source)
    base_head = gitutil.rev_parse(root, base)
    if gitutil.merge_base(root, base_head, source_head) == source_head:
        _mark_delivered(store, campaign)
        return [
            delivered_result(
                f"already contained in {base}", status="already_contained"
            )
        ]

    outcome = gitutil.merge_tree(root, base_head, source_head)
    if not outcome.clean:
        return [
            DeliveryResult(
                status="failed",
                detail=_conflict_detail(outcome),
            )
        ]

    # The trusted checks run through the single check runner: a fingerprint
    # that already reached a terminal verdict is honored as cached, so a
    # resumed campaign re-verifies from the cache instead of re-running.
    checks: list[CheckResult] = []
    if run_checks_flag:
        specs = checks_from_config(config)
        if specs:
            runner = _delivery_checks(store, root, config, campaign)
            row = runner.run(
                source="deliver",
                commit=source_head,
                checks=specs,
            )
            checks = _check_results(row)
            if row["status"] != "passed":
                return [
                    DeliveryResult(
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
            root, config, store, campaign=campaign, narrative=narrative
        )
        found = pullrequest.create(
            root, head=source, base=base, title=title, body=body
        )

    _mark_delivered(store, campaign)
    return [
        delivered_result(
            f"pull request opened: {found['url']}",
            found,
            checks=checks,
        )
    ]


def deliver(
    store: Store,
    root: Path,
    config: dict[str, Any],
    *,
    campaign: str | None = None,
    source: str | None = None,
    run_checks_flag: bool = True,
    mode: str | None = None,
    narrative: str | None = None,
) -> list[DeliveryResult]:
    """Deliver the campaign with the selected mode.

    ``local`` merges the campaign branch into the local base branch and stops.
    ``pull_request`` pushes the branch and opens a pull request.  Both modes
    keep the campaign branch and the campaign evidence.  ``narrative`` reaches
    the pull request body only, so the local mode ignores it.
    """
    resolved = resolve_delivery_mode(config, mode=mode)
    if resolved == "local":
        return deliver_local_merge(
            store,
            root,
            config,
            campaign=campaign,
            source=source,
            run_checks_flag=run_checks_flag,
        )
    return deliver_pull_request(
        store,
        root,
        config,
        campaign=campaign,
        source=source,
        run_checks_flag=run_checks_flag,
        narrative=narrative,
    )


def _require_base_not_behind_remote(
    root: Path, config: dict[str, Any], base: str
) -> None:
    """Refuse a local merge when the remote base is ahead or diverged.

    The local merge moves the base branch.  When the remote base already
    holds commits that the local base does not, a later user push would
    surprise the user, so the engine refuses first.
    """
    remote = (config.get("policy") or {}).get("remote") or "origin"
    ref = f"refs/remotes/{remote}/{base}"
    res = gitutil.git(root, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if not res.ok:
        return
    remote_head = res.stdout.strip()
    base_head = gitutil.rev_parse(root, base)
    if gitutil.merge_base(root, base_head, remote_head) != remote_head:
        raise SlicemeError(
            f"refusing a local merge: {remote}/{base} is ahead of or has "
            f"diverged from local '{base}'; update '{base}' first"
        )


def _merge_base_branch(root: Path, base: str, source: str, message: str) -> str:
    """Merge *source* into the local *base* branch and return the merge commit.

    The engine merges in the worktree that holds the base branch.  That keeps
    the user's checkout in sync.  When the base branch is not checked out, the
    engine merges in a temporary worktree, so the merge never disturbs any
    checkout.
    """
    entry = gitutil.worktree_for_branch(root, base)
    if entry is not None:
        if not gitutil.is_clean(entry.path):
            raise SlicemeError(
                f"refusing a local merge: the worktree for '{base}' has "
                "uncommitted changes"
            )
        return gitutil.merge_no_ff(entry.path, source, message)
    scratch = scratch_dir(root) / f"land-{abs(hash(base + source)) % 10_000_000}"
    try:
        gitutil.add_worktree(root, scratch, branch=base, base=base, new_branch=False)
        return gitutil.merge_no_ff(scratch, source, message)
    finally:
        gitutil.cleanup_worktree(root, scratch)


def deliver_local_merge(
    store: Store,
    root: Path,
    config: dict[str, Any],
    *,
    campaign: str | None = None,
    source: str | None = None,
    run_checks_flag: bool = True,
) -> list[DeliveryResult]:
    """Merge the campaign worktree branch into the local base branch.

    The merge stays local: sliceme pushes nothing and calls no forge.  The
    user pushes the base branch later.  The merge always creates a merge
    commit, so the campaign history stays visible.  The campaign branch is
    kept for provenance.  The campaign state moves to ``landed``.
    """
    campaign_branch = target_branch_of(config)
    if is_default_branch(root, campaign_branch, config):
        raise SlicemeError(
            f"refusing to deliver the default branch '{campaign_branch}' as the "
            "campaign branch; sliceme merges a feature branch only"
        )
    base = delivery_base_of(config)
    if campaign_branch == base:
        raise SlicemeError(
            f"refusing to deliver: the campaign branch '{campaign_branch}' equals "
            "the delivery base"
        )
    source = source or config.get("work_branch")
    if not source or not gitutil.branch_exists(root, source):
        raise SlicemeError(
            "no campaign worktree branch to deliver; run `sliceme open` first"
        )
    source = str(source)
    if not gitutil.branch_exists(root, base):
        raise SlicemeError(f"delivery base '{base}' does not exist")

    source_head = gitutil.rev_parse(root, source)
    base_head = gitutil.rev_parse(root, base)
    if gitutil.merge_base(root, base_head, source_head) == source_head:
        _mark_delivered(store, campaign, state="landed")
        return [
            local_result(
                f"already contained in local {base}", merge_commit=base_head
            )
        ]

    outcome = gitutil.merge_tree(root, base_head, source_head)
    if not outcome.clean:
        return [
            DeliveryResult(
                status="failed", detail=_conflict_detail(outcome), mode="local"
            )
        ]

    checks: list[CheckResult] = []
    if run_checks_flag:
        specs = checks_from_config(config)
        if specs:
            runner = _delivery_checks(store, root, config, campaign)
            row = runner.run(source="deliver", commit=source_head, checks=specs)
            checks = _check_results(row)
            if row["status"] != "passed":
                return [
                    DeliveryResult(
                        status="failed",
                        detail="checks failed; local merge not applied",
                        checks=checks,
                        mode="local",
                    )
                ]

    _require_base_not_behind_remote(root, config, base)
    message = f"sliceme: merge {source} into {base}"
    merge_commit = _merge_base_branch(root, base, source, message)
    _mark_delivered(store, campaign, state="landed")
    return [
        local_result(
            f"merged {source} into local {base}; push {base} when ready",
            merge_commit=merge_commit,
            checks=checks,
        )
    ]


# ---------------------------------------------------------------------------
# Candidate ordering and phase projection
# ---------------------------------------------------------------------------
@dataclass
class Phase:
    index: int
    records: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.index,
            "members": [
                {"record": c["id"], "node": c.get("node"), "commit": c.get("head_commit")}
                for c in self.records
            ],
        }


def _phase_index_by_node(root: Path, config: dict[str, Any]) -> dict[str, int]:
    """Map every DAG node id to its phase index (empty for a non-campaign plane)."""
    branch = config.get("work_branch")
    dag = campaign.load_dag(root, branch) if branch else None
    if not dag or not dag.get("nodes"):
        return {}
    phase_size = int(dag.get("concurrency") or DEFAULT_PHASE_SIZE)
    planned = plan_dag_phases(list(dag["nodes"]), phase_size=phase_size)
    return {member: w.index for w in planned for member in w.members}


def plan_phases(
    store: Store,
    root: Path,
    config: dict[str, Any],
    records: list[dict[str, Any]],
) -> list[Phase]:
    """Group records into DAG phases, each internally priority-ordered."""
    if not records:
        return []
    phase_of = _phase_index_by_node(root, config)
    fallback = (max(phase_of.values()) + 1) if phase_of else 0

    def key(record: dict[str, Any]) -> tuple[Any, ...]:
        node = str(record.get("node") or "")
        return (
            phase_of.get(node, fallback),
            -int(record.get("priority") or 0),
            float(record.get("created_at") or 0.0),
            int(record["id"]),
        )

    ordered = sorted(records, key=key)
    phases: list[Phase] = []
    by_index: dict[int, Phase] = {}
    for record in ordered:
        node = str(record.get("node") or "")
        index = phase_of.get(node, fallback)
        phase = by_index.get(index)
        if phase is None:
            phase = Phase(index=index)
            by_index[index] = phase
            phases.append(phase)
        phase.records.append(record)
    phases.sort(key=lambda w: w.index)
    return phases

