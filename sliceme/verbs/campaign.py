"""Campaign worktree, wave, and check verbs for the sliceme engine.

Internal module: :class:`sliceme.service.Service` composes this mixin and
:func:`sliceme.surface.dispatch` stays the one verb facade.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import campaign, gitutil, integrate, sandbox
from ..ownership import (
    DEFAULT_WAVE_SIZE,
    node_owns,
    plan_dag_waves,
    strongest_gpu_tier,
    validate_dag,
)
from ..store import Store
from ..util import (
    SlicemeError,
    worktrees_dir,
)
from .support import (
    _changed_entries,
    _describe_violation,
    _owners_of,
    _record_error,
    _subject_line,
)


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
        key = str(campaign_row["key"])
        name = str(campaign_row["unit_name"])
        existing = self.store.get_unit_by_campaign(key)
        if existing is not None:
            worktree = Path(existing["worktree"])
            if worktree.exists():
                return existing
            return self._recreate_campaign_worktree(existing, base=base)
        config = self.config
        base_ref, fallback = self._delivery_base_ref(base)
        base_commit = gitutil.rev_parse(self.root, base_ref)
        # Single-source the branch with every other consumer (delivery, review,
        # evidence): the recorded worktree branch.  A new row keeps
        # worktree_branch equal to target_branch, so the fallback only covers a
        # row migrated from the older two-branch model.
        branch = str(
            campaign_row.get("worktree_branch") or campaign_row["target_branch"]
        )
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
        try:
            unit_id = self.store.create_unit(
                name=name,
                kind="campaign",
                worktree=str(worktree),
                branch=branch,
                base_commit=base_commit,
                campaign=key,
            )
        except Exception:
            gitutil.cleanup_worktree(self.root, worktree)
            raise
        self.store.conn.commit()
        unit = self.store.get_unit(unit_id)
        if unit is None:  # pragma: no cover - the insert just succeeded
            return unit  # type: ignore[return-value]
        if fallback:
            unit = {**unit, "base_fallback": fallback}
        return unit  # type: ignore[return-value]

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
        self, existing: dict[str, Any], *, base: str | None = None
    ) -> dict[str, Any]:
        """Recreate a campaign worktree whose directory was deleted by hand."""
        worktree = Path(existing["worktree"])
        branch = str(existing["branch"])
        gitutil.cleanup_worktree(self.root, worktree)
        if gitutil.branch_exists(self.root, branch):
            gitutil.add_worktree(
                self.root, worktree, branch=branch, base=branch, new_branch=False
            )
        else:
            base_ref = (
                base
                or existing.get("base_commit")
                or self.config.get("base")
                or self.config.get("target_branch")
                or "main"
            )
            gitutil.add_worktree(
                self.root, worktree, branch=branch, base=gitutil.rev_parse(self.root, base_ref)
            )
        key = existing.get("campaign")
        if key:
            return self.store.get_unit_by_campaign(str(key))  # type: ignore[return-value]
        return self.store.get_unit(existing["name"])  # type: ignore[return-value]

    def create_wave_workspace(
        self, wave_index: int, *, base: str | None = None
    ) -> dict[str, Any]:
        """Compatibility alias: every wave shares the one campaign worktree."""
        return self.create_campaign_workspace(base=base)

    def wave_unit(self, wave_index: int) -> dict[str, Any] | None:
        campaign_row = self.campaign
        if campaign_row is None:
            return self.store.get_unit("campaign")
        return self.store.get_unit_by_campaign(str(campaign_row["key"]))

    def record_wave(
        self,
        wave_index: int,
        *,
        only: str | list[str] | None = None,
        messages: dict[str, str] | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """Record a shared wave worktree: conformance, then per-node commits.

        Without *only*, every changed path is attributed to exactly one
        same-wave node by its owned directories; each node gets one commit and
        a prepared candidate on the shared wave branch, and any violation is a
        wave-wide error.  The commit subject is the node's per-node ``messages``
        entry.

        With *only* (one node id, or a list for a batch) the record is scoped to
        those nodes: every changed path is attributed across the whole DAG and a
        path owned by another node is ignored.  A path owned by no node
        (``stray_path``) or by more than one (``ambiguous_path``) has no single
        owner, so it is reported by the wave's first declared member; recording
        any other member ignores it.  A cross-node rename fails only its source
        node.  Every violation raises a :class:`SlicemeError` whose ``reason``
        attribute is a machine-readable code (``stray_path``,
        ``ambiguous_path``, ``cross_node_rename``, ``missing_description``);
        callers switch on the code, never the text.
        """
        branch = self.config.get("main_branch")
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
        if unit is None:
            raise SlicemeError(
                f"wave {wave_index} has no workspace; run `sliceme wave --open` first"
            )
        by_id = {str(node["id"]): node for node in dag["nodes"]}
        if only is None:
            members = [by_id[node_id] for node_id in wave.members if node_id in by_id]
            return self._record_wave_commits(
                unit,
                int(wave_index),
                members,
                messages=messages,
                summary=summary,
            )
        requested = [only] if isinstance(only, str) else list(only)
        targets: list[str] = []
        for item in requested:
            node_id = str(item or "").strip()
            if not node_id:
                continue
            if node_id not in by_id:
                raise SlicemeError(f"record --only: unknown node '{node_id}'")
            if node_id not in wave.members:
                raise SlicemeError(
                    f"record --only: node '{node_id}' is not in wave {wave_index}"
                )
            if node_id not in targets:
                targets.append(node_id)
        if not targets:
            raise SlicemeError("record --only needs a node id")
        members = [by_id[node_id] for node_id in targets]
        return self._record_wave_commits(
            unit,
            int(wave_index),
            members,
            messages=messages,
            summary=summary,
            dag_nodes=list(dag["nodes"]),
            wave_order=list(wave.members),
        )

    def _record_wave_commits(
        self,
        unit: dict[str, Any],
        wave_index: int,
        members: list[dict[str, Any]],
        *,
        messages: dict[str, str] | None,
        summary: str | None,
        dag_nodes: list[dict[str, Any]] | None = None,
        wave_order: list[str] | None = None,
    ) -> dict[str, Any]:
        worktree = Path(unit["worktree"])
        if not worktree.exists():
            raise SlicemeError(f"wave worktree missing: {worktree}")
        gitutil.git(worktree, "add", "-A", check=False)
        # Diff against the current HEAD, not the fork point, so an earlier
        # wave's committed changes are not re-attributed to this wave.  The
        # campaign worktree accumulates commits across waves.
        entries = _changed_entries(worktree)
        # ``dag_nodes`` turns on the scoped record: attribute every changed path
        # across the whole DAG and fail only the recorded node.  Without it the
        # attribution stays per wave member (today's wave-wide record).
        targeted = dag_nodes is not None
        owner_nodes = dag_nodes if targeted else members
        owners = {str(node["id"]): node_owns(node) for node in owner_nodes}
        assigned_to = {str(node["id"]) for node in members}
        # A stray or ambiguous path belongs to no single node.  Attribute it to
        # the wave's first declared member so exactly one scoped record reports
        # it, instead of every member's record failing on the same path.
        reporter = str((wave_order or [""])[0]) if targeted else None
        assignment: dict[str, list[str]] = {node_id: [] for node_id in assigned_to}
        violations: list[tuple[str, str]] = []
        for status, path, old in entries:
            new_owners = _owners_of(path, owners)
            old_owners = _owners_of(old, owners) if old else []
            if len(new_owners) == 0:
                if targeted and reporter not in assigned_to:
                    continue
                violations.append(
                    ("stray_path", _describe_violation(status, path, old, new_owners, old_owners))
                )
                continue
            if len(new_owners) > 1:
                if targeted:
                    responsible = next(
                        (nid for nid in (wave_order or []) if nid in new_owners),
                        reporter,
                    )
                    if responsible not in assigned_to:
                        continue
                violations.append(
                    (
                        "ambiguous_path",
                        _describe_violation(status, path, old, new_owners, old_owners),
                    )
                )
                continue
            node_id = new_owners[0]
            if old and (len(old_owners) != 1 or old_owners[0] != node_id):
                # A cross-node rename fails only its source node (the old
                # owner).  A destination record ignores the rename.
                source = old_owners[0] if len(old_owners) == 1 else None
                if not targeted or (source is not None and source in assigned_to):
                    violations.append(
                        (
                            "cross_node_rename",
                            _describe_violation(status, path, old, new_owners, old_owners),
                        )
                    )
                continue
            if node_id in assignment:
                assignment[node_id].append(path)
                if old:
                    assignment[node_id].append(old)
            # A path owned by another DAG node is ignored for this record.
        if violations:
            reason, detail = violations[0]
            if targeted:
                recorded = ", ".join(str(node["id"]) for node in members)
                raise _record_error(
                    reason, f"record --only={recorded}: {detail}"
                )
            raise _record_error(
                reason,
                "wave conformance failed; every changed path must map to exactly one "
                "wave node's owned directories: "
                + "; ".join(detail for _, detail in violations[:10]),
            )
        messages = messages or {}
        created: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for node in members:
            node_id = str(node["id"])
            paths = sorted(set(assignment.get(node_id) or []))
            if not paths:
                continue
            # The subject is the human description, never a wave prefix.  A
            # node with changes must have its own description; Sliceme never
            # invents one.
            description = _subject_line(messages.get(node_id))
            if not description:
                raise _record_error(
                    "missing_description",
                    f"wave --record: node '{node_id}' has no description; "
                    f"pass --messages '{{\"{node_id}\": \"...\"}}'",
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
            cid = self.store.create_candidate(
                unit_id=int(unit["id"]),
                head_commit=head,
                summary=summary,
                node=node_id,
                campaign=self.campaign_key(),
            )
            created.append(self.store.get_candidate(cid))
            results.append(
                {"node": node_id, "reason": "ok", "paths": paths, "candidate": int(cid)}
            )
        self.store.conn.commit()
        return {
            "wave": int(wave_index),
            "unit": unit["name"],
            "branch": unit["branch"],
            "worktree": str(worktree),
            "candidates": created,
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

        branch = self.config.get("main_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        return CheckRunner(
            self.root, self.store, self.config, dag=dag, campaign=self.campaign_key()
        )

    def current_wave_index(self) -> int:
        """The campaign's current wave index, read from the engine's own state."""
        row = self.require_campaign()
        index, _members = self._current_wave(str(row["target_branch"]))
        if index is None:
            raise SlicemeError("no current wave in this campaign")
        return int(index)

    def check_wave(self) -> dict[str, Any]:
        """Run the trusted checks on the recorded wave's combined tree.

        The wave tree is the campaign worktree head after ``wave --record``, so
        the checks run over the recorded nodes together.  The recorded wave is
        the planned wave that the newest recorded candidate belongs to, not the
        next wave that ``ready`` offers.  A fingerprint that already has a
        terminal verdict is served from the cache.  The check vector is the
        plane's trusted checks plus the union of the wave members'
        ``acceptance`` commands, and the wave's strongest GPU tier selects the
        runner and its fail-closed gate.
        """
        row = self.require_campaign()
        branch = str(row["target_branch"])
        index, members = self._recorded_wave(branch)
        if index is None:
            raise SlicemeError("no current wave in this campaign")
        dag = campaign.load_dag(self.root, branch) or {}
        by_id = {str(node["id"]): node for node in dag.get("nodes") or []}
        member_nodes = [by_id[m] for m in members if m in by_id]
        source = str(self.config.get("worktree_branch") or branch)
        result = self.checks().run(
            source=f"wave:{index}",
            commit=source,
            checks=self._wave_checks(member_nodes),
            gpu=strongest_gpu_tier(member_nodes),
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
        branch = self.config.get("main_branch")
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
