"""Campaign worktree, wave, and check verbs for the sliceme engine.

Internal module: :class:`sliceme.service.Service` composes this mixin and
:func:`sliceme.surface.dispatch` stays the one verb facade.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import campaign, gitutil, integrate, sandbox
from ..gitutil import _changed_entries, _record_error
from ..ownership import (
    DEFAULT_WAVE_SIZE,
    node_owns,
    owners_of,
    plan_dag_waves,
    strongest_gpu_tier,
    validate_dag,
)
from ..store import Store
from ..util import (
    SlicemeError,
    worktrees_dir,
)


def _subject_line(text: str | None) -> str:
    """Return the first non-empty, whitespace-collapsed line of *text*."""
    for line in str(text or "").splitlines():
        collapsed = " ".join(line.split())
        if collapsed:
            return collapsed
    return ""


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


class CampaignVerbs:

    def create_campaign_workspace(self, *, base: str | None = None) -> dict[str, Any]:
        """Create (or reuse) the campaign worktree for the bound campaign.

        Every wave commits onto this one branch.  It is never recreated and
        never rebased, so files written by an earlier wave are still present
        when the next wave runs.
        """
        campaign_row = self.campaign
        if campaign_row is None:
            campaign_row = self._ensure_legacy_campaign()
        if campaign_row is None:
            raise SlicemeError("no campaign in this plane; run `sliceme start` first")
        # The bound campaign row is cached on the service, so refresh it before
        # reading the worktree that an earlier ``open`` recorded.
        campaign_row = self.store.get_campaign(str(campaign_row["key"])) or campaign_row
        key = str(campaign_row["key"])
        # The campaign owns one worktree; read it from the campaign row.
        recorded = campaign_row.get("worktree")
        if recorded:
            worktree = Path(recorded)
            if worktree.exists():
                return self._writer_of(campaign_row)
            return self._recreate_campaign_worktree(campaign_row, base=base)
        config = self.config
        base_ref, fallback = self._delivery_base_ref(base)
        base_commit = gitutil.rev_parse(self.root, base_ref)
        # Single-source the branch with every other consumer (delivery, review,
        # evidence): the recorded work branch.  A new row keeps the work branch
        # equal to the campaign branch, so the fallback only covers a row
        # migrated from the older two-branch model.
        branch = str(campaign_row["work_branch"])
        if integrate.is_default_branch(self.root, branch, config):
            raise SlicemeError(
                f"refusing to use the default branch '{branch}' as the campaign worktree"
            )
        worktree = worktrees_dir(self.root) / f"campaign-{key}"
        gitutil.cleanup_worktree(self.root, worktree)
        if gitutil.branch_exists(self.root, branch):
            gitutil.add_worktree(
                self.root, worktree, branch=branch, base=branch, new_branch=False
            )
        else:
            gitutil.add_worktree(self.root, worktree, branch=branch, base=base_commit)
        self.store.set_campaign_worktree(key, str(worktree))
        writer = self._writer_of(self.store.get_campaign(key))
        if fallback:
            writer = {**writer, "base_fallback": fallback}
        return writer

    @staticmethod
    def _writer_of(row: dict[str, Any]) -> dict[str, Any]:
        """The campaign's one worktree writer, in the old unit shape."""
        return {
            "id": row.get("id"),
            "name": row.get("unit_name") or row.get("key"),
            "kind": "campaign",
            "worktree": row.get("worktree"),
            "branch": row.get("work_branch"),
            "state": row.get("state"),
            "campaign": row.get("key"),
        }

    def _delivery_base_ref(self, base: str | None) -> tuple[str, str | None]:
        """The ref the campaign worktree starts from, and any fallback note.

        The worktree starts at the newest delivery base on the remote, so it
        holds commits the local default branch may not have.  An explicit
        *base* wins.  When the remote or the delivery base is absent, the local
        delivery base is used and the fallback is reported to the caller.
        """
        if base:
            return base, None
        config = self.config
        delivery_base = integrate.delivery_base_of(config)
        remote = (config.get("policy") or {}).get("remote") or "origin"
        fetched = f"{remote}/{delivery_base}"
        if gitutil.fetch(self.root, remote, delivery_base).ok:
            return fetched, None
        if not gitutil.branch_exists(self.root, delivery_base):
            raise SlicemeError(
                f"cannot resolve the delivery base '{delivery_base}': "
                f"fetching {fetched} failed and no local branch exists"
            )
        return delivery_base, f"could not fetch {fetched}; using local {delivery_base}"

    def _recreate_campaign_worktree(
        self, row: dict[str, Any], *, base: str | None = None
    ) -> dict[str, Any]:
        """Recreate a campaign worktree whose directory was deleted by hand."""
        worktree = Path(row["worktree"])
        branch = str(row["work_branch"])
        gitutil.cleanup_worktree(self.root, worktree)
        if gitutil.branch_exists(self.root, branch):
            gitutil.add_worktree(
                self.root, worktree, branch=branch, base=branch, new_branch=False
            )
        else:
            base_ref = (
                base
                or self.config.get("base_branch")
                or self.config.get("work_branch")
                or "main"
            )
            gitutil.add_worktree(
                self.root, worktree, branch=branch, base=gitutil.rev_parse(self.root, base_ref)
            )
        return self._writer_of(self.store.get_campaign(str(row["key"])))

    def create_wave_workspace(
        self, wave_index: int, *, base: str | None = None
    ) -> dict[str, Any]:
        """Compatibility alias: every wave shares the one campaign worktree."""
        return self.create_campaign_workspace(base=base)

    def wave_unit(self, wave_index: int) -> dict[str, Any] | None:
        row = self.campaign
        if row is None:
            return None
        fresh = self.store.get_campaign(str(row["key"])) or row
        return self._writer_of(fresh)

    def campaign_worktree(self) -> Path | None:
        """The campaign's one worktree path (``campaigns.worktree``).

        The campaign owns one worktree, so the path is read from the campaign
        row.
        """
        row = self.campaign
        if row is None:
            return None
        fresh = self.store.get_campaign(str(row["key"])) or row
        recorded = fresh.get("worktree")
        return Path(recorded) if recorded else None

    def record_wave(
        self,
        wave_index: int,
        *,
        messages: dict[str, str] | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """Record the current wave: conformance, then per-node commits.

        Every changed path is attributed to exactly one same-wave node by its
        owned directories; each such node gets one commit and a record on the
        campaign branch.  A wave member that changed no file records an
        explicit empty marker so the wave still completes.  The commit subject
        falls back to the DAG node ``label``.  A path owned by no node
        (``stray_path``) or by more than one (``ambiguous_path``) fails the
        wave.  Every violation raises a :class:`SlicemeError` whose ``reason``
        attribute is a machine-readable code (``stray_path``,
        ``ambiguous_path``, ``cross_node_rename``, ``missing_description``);
        callers switch on the code, never the text.
        """
        branch = self.config.get("work_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        if not dag or not dag.get("nodes"):
            raise SlicemeError("record_wave needs a campaign DAG")
        validate_dag(list(dag["nodes"]))
        wave_size = int(dag.get("concurrency") or DEFAULT_WAVE_SIZE)
        waves = plan_dag_waves(list(dag["nodes"]), wave_size=wave_size)
        wave = next((w for w in waves if w.index == int(wave_index)), None)
        if wave is None:
            raise SlicemeError(f"unknown wave: {wave_index}")
        unit = self.wave_unit(wave_index)
        if unit is None or not unit.get("worktree"):
            raise SlicemeError(
                f"wave {wave_index} has no workspace; run `sliceme open` first"
            )
        by_id = {str(node["id"]): node for node in dag["nodes"]}
        members = [by_id[node_id] for node_id in wave.members if node_id in by_id]
        return self._record_wave_commits(
            unit,
            int(wave_index),
            members,
            messages=messages,
            summary=summary,
        )

    def _record_wave_commits(
        self,
        unit: dict[str, Any],
        wave_index: int,
        members: list[dict[str, Any]],
        *,
        messages: dict[str, str] | None,
        summary: str | None,
    ) -> dict[str, Any]:
        worktree = self.campaign_worktree() or Path(unit["worktree"])
        if not worktree.exists():
            raise SlicemeError(f"wave worktree missing: {worktree}")
        gitutil.git(worktree, "add", "-A", check=False)
        # Diff against the current HEAD, not the fork point, so an earlier
        # wave's committed changes are not re-attributed to this wave.  The
        # campaign worktree accumulates commits across waves.
        entries = _changed_entries(worktree)
        owners = {str(node["id"]): node_owns(node) for node in members}
        assignment: dict[str, list[str]] = {node_id: [] for node_id in owners}
        violations: list[tuple[str, str]] = []
        for status, path, old in entries:
            new_owners = owners_of(path, owners)
            old_owners = owners_of(old, owners) if old else []
            if len(new_owners) == 0:
                violations.append(
                    ("stray_path", _describe_violation(status, path, old, new_owners, old_owners))
                )
                continue
            if len(new_owners) > 1:
                violations.append(
                    (
                        "ambiguous_path",
                        _describe_violation(status, path, old, new_owners, old_owners),
                    )
                )
                continue
            node_id = new_owners[0]
            if old and (len(old_owners) != 1 or old_owners[0] != node_id):
                violations.append(
                    (
                        "cross_node_rename",
                        _describe_violation(status, path, old, new_owners, old_owners),
                    )
                )
                continue
            assignment[node_id].append(path)
            if old:
                assignment[node_id].append(old)
        if violations:
            reason, detail = violations[0]
            raise _record_error(
                reason,
                "wave conformance failed; every changed path must map to exactly one "
                "wave node's owned directories: "
                + "; ".join(detail for _, detail in violations[:10]),
            )
        messages = messages or {}
        created: list[dict[str, Any]] = []
        empties: list[str] = []
        results: list[dict[str, Any]] = []
        for node in members:
            node_id = str(node["id"])
            paths = sorted(set(assignment.get(node_id) or []))
            if not paths:
                # A wave member that changed no file records an explicit empty
                # marker: the wave still completes, so the cursor advances
                # instead of returning the same member until the loop cap stops
                # it.  The marker is not a commit, so it never contributes its
                # acceptance commands to the wave check vector.
                self.store.create_record(
                    head_commit=gitutil.head_commit(worktree),
                    summary=None,
                    node=node_id,
                    campaign=self.campaign_key(),
                    empty=True,
                )
                empties.append(node_id)
                continue
            # The subject is the human description, never a wave prefix.  A
            # node with changes must have its own description; Sliceme never
            # invents one.  Fall back to the DAG node ``label``, which the
            # planner authors, so the trusted resource records without a
            # per-node description argument.
            description = _subject_line(
                messages.get(node_id) or node.get("label")
            )
            if not description:
                raise _record_error(
                    "missing_description",
                    f"record: node '{node_id}' has no description; "
                    "give the DAG node a label",
                )
            result = gitutil.git(
                worktree, "commit", "-m", description, "--", *paths, check=False
            )
            if not result.ok:
                raise SlicemeError(
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"commit failed for {node_id}"
                )
            head = gitutil.head_commit(worktree)
            cid = self.store.create_record(
                head_commit=head,
                summary=summary,
                node=node_id,
                campaign=self.campaign_key(),
            )
            created.append(self.store.get_record(cid))
            results.append(
                {"node": node_id, "reason": "ok", "paths": paths, "record": int(cid)}
            )
        self.store.conn.commit()
        return {
            "wave": int(wave_index),
            "unit": unit["name"],
            "branch": unit["branch"],
            "worktree": str(worktree),
            "records": created,
            "empty": empties,
            "changed": [path for _, path, _ in entries],
            "reason": "ok",
            "results": results,
        }

    def simulation(self, *, run_checks_flag: bool = True) -> dict[str, Any]:
        return integrate.simulate(
            self.store,
            self.root,
            self.config,
            campaign=self.campaign_key(),
            run_checks_flag=run_checks_flag,
        )

    def checks(self):
        """Build the single synchronous check runner for this plane."""
        from ..checks import CheckRunner

        branch = self.config.get("work_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        return CheckRunner(
            self.root, self.store, self.config, dag=dag, campaign=self.campaign_key()
        )

    def current_wave_index(self) -> int:
        """The campaign's current wave index, read from the engine's own state."""
        row = self.require_campaign()
        index = self._wave_cursor(str(row["work_branch"])).index
        if index is None:
            raise SlicemeError("no current wave in this campaign")
        return int(index)

    def check_wave(self) -> dict[str, Any]:
        """Run the trusted checks on the recorded wave's combined tree.

        The wave tree is the campaign worktree head after ``record``, so
        the checks run over the recorded nodes together.  The recorded wave is
        the planned wave that the newest recorded record belongs to, not the
        next wave that ``ready`` offers.  A fingerprint that already has a
        terminal verdict is served from the cache.  The check vector is the
        plane's trusted checks plus the union of the wave members'
        ``acceptance`` commands, and the wave's strongest GPU tier selects the
        runner and its fail-closed gate.
        """
        row = self.require_campaign()
        branch = str(row["work_branch"])
        cursor = self._wave_cursor(branch)
        index, members = cursor.recorded_index, cursor.recorded_members
        if index is None:
            raise SlicemeError("no current wave in this campaign")
        dag = campaign.load_dag(self.root, branch) or {}
        by_id = {str(node["id"]): node for node in dag.get("nodes") or []}
        member_nodes = [by_id[m] for m in members if m in by_id]
        # The acceptance vector is committed-only, but the GPU tier reflects the
        # wave's full planned membership, so an all-empty wave keeps its tier.
        planned_members = cursor.recorded_planned_members or members
        planned_nodes = [by_id[m] for m in planned_members if m in by_id]
        source = str(self.config.get("work_branch") or branch)
        result = self.checks().run(
            source=f"wave:{index}",
            commit=source,
            checks=self._wave_checks(member_nodes),
            gpu=strongest_gpu_tier(planned_nodes),
            wave=index,
        )
        return {"wave": index, "members": members, **result}

    def _wave_checks(self, member_nodes: list[dict[str, Any]]) -> list[Any]:
        """The trusted checks plus the wave members' acceptance commands.

        Order-preserving and de-duplicated by command text: the plane's checks
        come first, then each node's ``acceptance`` commands in wave order.
        """
        from ..verifier import acceptance_checks, checks_from_config

        specs = list(checks_from_config(self.config))
        seen = {spec.command for spec in specs}
        acceptance: list[str] = []
        for node in member_nodes:
            for command in node.get("acceptance") or []:
                text = str(command)
                if text and text not in seen:
                    seen.add(text)
                    acceptance.append(text)
        return specs + acceptance_checks(acceptance)

    def sandbox_info(self, *, gpu_required: bool = False) -> dict[str, Any]:
        """Resolve and validate the project sandbox gate (never raises)."""
        branch = self.config.get("work_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        required = sandbox.is_required(dag, self.config)
        try:
            profile = sandbox.require_sandbox(
                dag, self.config, root=self.root, gpu_required=gpu_required
            )
        except SlicemeError as exc:
            return {"ok": False, "required": required, "error": str(exc)}
        return {
            "ok": True,
            "required": required,
            "manifest": profile.manifest,
            "digest": profile.digest(),
            "sandbox": profile.to_dict(),
        }
