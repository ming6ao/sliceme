"""Status, ready, and plan verbs for the sliceme engine.

Internal module: :class:`sliceme.service.Service` composes this mixin and
:func:`sliceme.surface.dispatch` stays the one verb facade.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from .. import campaign, integrate
from ..campaign import dag_path as _campaign_dag_path
from ..ownership import (
    DEFAULT_PHASE_SIZE,
    merge_same_own_nodes,
    plan_dag_phases,
    validate_dag,
)
from ..util import (
    SlicemeError,
    write_json,
)


class PhaseCursor(NamedTuple):
    """The one phase cursor that drives ``ready``, ``record``, and ``check``.

    ``index``/``members`` name the phase to work on: the first planned phase that
    is not fully recorded, or the last phase when every phase is recorded.  A
    member that recorded zero changes still counts as recorded (an empty
    record), so the cursor advances past a no-op phase instead of stalling.

    ``recorded_index``/``recorded_members`` name the phase ``check`` runs: the
    phase the newest record belongs to, scoped to the members with a real
    commit.  ``record`` commits the current phase and ``check`` runs right after
    it, so check reads the phase just recorded, not the next one.
    """

    index: int | None
    members: list[str]
    recorded_index: int | None
    recorded_members: list[str]


class StatusVerbs:

    def ready_nodes(self) -> list[str]:
        """The ids of the current phase's nodes ready to spawn.

        A phase is the one scheduling gate: every dependency lives in an earlier
        phase (``plan_dag_phases`` sets ``min_phase = max(phase(dep) + 1)``), so a
        member is ready once its own status is neither finished nor running.
        A missing DAG has no ready nodes.
        """
        branch = self.config.get("work_branch")
        if not branch:
            return []
        return self._ready_in_current_phase(branch)

    def paused(self) -> bool:
        """True when the bound campaign's cooperative pause flag is set.

        The flag is the ``<branch-key>.control.json`` file
        ``campaign.load_control`` reads.  ``status`` and ``ready`` carry the
        value so a caller can stop before doing work without reading the file
        itself.
        """
        row = self.campaign
        if row is None:
            return False
        control = campaign.load_control(self.root, str(row["work_branch"]))
        return bool(control and control.get("pause"))

    def _recorded_nodes(self) -> set[str]:
        """The node ids with a record, empty or not.

        A record (a committed node or an explicit empty marker) is the durable
        evidence that a phase member finished, so the phase cursor advances.
        """
        return {
            str(record["node"])
            for record in self.store.list_records(campaign=self.campaign_key())
            if record.get("node")
        }

    def _committed_nodes(self) -> set[str]:
        """The node ids with a real commit (a record that changed files).

        An explicit empty record is not a commit, so it never contributes its
        acceptance commands to the phase check vector.
        """
        return {
            str(record["node"])
            for record in self.store.list_records(campaign=self.campaign_key())
            if record.get("node") and not record.get("empty")
        }

    def _descriptor_nodes(self, branch: str) -> dict[str, str]:
        """The node statuses the adapter recorded in the session descriptor.

        The descriptor is the one place a suspended node's ``running`` status
        lives; the engine derives ``recorded`` from the SQLite records.
        """
        descriptor = campaign.load_session(self.root, branch) or {}
        found: dict[str, str] = {}
        for node, entry in campaign.descriptor_nodes(descriptor).items():
            status = entry.get("status") if isinstance(entry, dict) else entry
            if status:
                found[str(node)] = str(status)
        return found

    def _finished_nodes(self) -> set[str]:
        """The node ids the phase cursor treats as done: the recorded nodes."""
        return set(self._recorded_nodes())

    def _node_states(self, branch: str) -> dict[str, Any]:
        """The per-node status for display: recorded nodes are ``recorded``.

        The node vocabulary is ``pending``, ``running``, ``recorded``, and
        ``failed``.  The phase verdict carries pass/fail, so a recorded node is
        ``recorded`` whether or not the combined-tree check has run.  A node the
        descriptor recorded as running stays running.
        """
        merged: dict[str, Any] = {}
        for node, status in self._descriptor_nodes(branch).items():
            if status == "done":
                status = "recorded"
            merged[node] = {"status": status}
        for node in self._recorded_nodes():
            merged[node] = {**merged.get(node, {}), "status": "recorded"}
        return merged

    def _phase_rows(self, branch: str) -> list[dict[str, Any]]:
        """The phase projection for the ``status_summary`` reads.

        The scheduler's DAG phases are projected, and a phase is ``done`` when
        every member is finished, so the engine advances without a state file.
        """
        planned, _error = self._dag_phases(branch)
        finished = self._finished_nodes()
        rows = []
        for entry in planned:
            members = [str(m) for m in entry.get("members") or []]
            status = "done" if members and set(members) <= finished else "pending"
            rows.append(
                {"phase": int(entry.get("phase", 0)), "status": status, "members": members}
            )
        return rows

    def _phase_cursor(self, branch: str) -> PhaseCursor:
        """The one cursor for ``ready``, ``record``, and ``check``.

        The DAG projection plus the records in SQLite derive both positions:
        the phase to work on (first not fully recorded, else the last) and the
        phase ``check`` runs (the phase of the newest record, scoped to the
        members with a real commit).  See :class:`PhaseCursor`.
        """
        planned, _error = self._dag_phases(branch)
        finished = self._finished_nodes()
        committed = self._committed_nodes()
        index = next(
            (
                int(entry.get("phase", 0))
                for entry in planned
                if not set(str(m) for m in entry.get("members") or []) <= finished
            ),
            None,
        )
        if index is None and planned:
            index = int(planned[-1].get("phase", 0))
        members = next(
            (
                [str(m) for m in entry.get("members") or []]
                for entry in planned
                if int(entry.get("phase", 0)) == index
            ),
            [],
        )
        recorded_index, recorded_members = self._recorded_phase_of(
            planned, committed
        )
        return PhaseCursor(index, members, recorded_index, recorded_members)

    def _recorded_phase_of(
        self, planned: list[dict[str, Any]], committed: set[str]
    ) -> tuple[int | None, list[str]]:
        """The phase ``check`` runs: the phase of the newest record.

        ``record`` commits the finished nodes, then ``check`` runs against that
        same tree, so check names the phase the record just committed, not the
        next phase ``ready`` offers.  The newest record selects the phase; the
        phase's committed members form the acceptance vector, so an empty record
        contributes nothing.  When that phase has no committed member at all the
        vector is empty.  The planned members are the fallback only when nothing
        is recorded yet, and the first planned phase when the DAG has no records.
        """
        newest: str | None = None
        for record in self.store.list_records(campaign=self.campaign_key()):
            node = record.get("node")
            if node:
                newest = str(node)  # ascending id order: last wins
        if newest is not None:
            for entry in planned:
                members = [str(m) for m in entry.get("members") or []]
                if newest in members:
                    phase_members = [m for m in members if m in committed]
                    return int(entry.get("phase", 0)), phase_members
        if not planned:
            return None, []
        first = planned[0]
        first_members = [str(m) for m in first.get("members") or []]
        return int(first.get("phase", 0)), first_members

    def _ready_in_current_phase(self, branch: str) -> list[str]:
        """The current phase's members that are neither finished nor running."""
        cursor = self._phase_cursor(branch)
        if cursor.index is None or not cursor.members:
            return []
        finished = self._finished_nodes()
        states = self._node_states(branch)
        return [
            node_id
            for node_id in cursor.members
            if node_id not in finished
            and campaign.node_status(states, node_id) != "running"
        ]

    def ready(self) -> dict[str, Any]:
        """The current phase's ready node ids, its index, and the pause flag.

        The phase is the one scheduling gate: a node is ready when it is a
        member of the current phase and its own status is neither finished nor
        running.  The result is scoped to the current phase, so a caller spawns
        and records one phase per call.  A missing or malformed DAG reports no
        ready nodes instead of crashing the read.
        """
        row = self.campaign
        if row is None:
            return {"campaign": None, "ready": [], "phase": None, "paused": False}
        # Defensive merge first, so a shared directory becomes one node before
        # the one-visit validation runs.
        self.normalize_dag()
        branch = str(row["work_branch"])
        # A malformed DAG must stop the loop, not report an empty phase.  The
        # resource treats a failed verb as fatal, so the error reaches the user.
        _planned, error = self._dag_phases(branch)
        if error:
            raise SlicemeError(error)
        cursor = self._phase_cursor(branch)
        return {
            "campaign": row.get("name") or row["key"],
            "ready": self._ready_in_current_phase(branch),
            "phase": cursor.index,
            "paused": self.paused(),
        }

    def status(self) -> dict[str, Any]:
        # A plane with several campaigns and no reference reports the plane,
        # not one campaign.  A bound campaign reports its own slice.
        if self._campaign_ref is None:
            working = self.store.list_campaigns(state="working")
            if len(working) > 1:
                return self._plane_status()
        campaign = self.campaign
        if campaign is None:
            return self._plane_status()
        key = campaign["key"]
        branch = self.config.get("work_branch")
        # Normalization is required, so it runs before every projection.
        try:
            dag_merge = self.normalize_dag()
        except SlicemeError as exc:
            dag_merge = {"merged": {}, "error": str(exc)}
        units = [self._project_campaign(campaign)]
        records = self.store.list_records(campaign=key)
        phases = integrate.plan_phases(
            self.store,
            self.root,
            self.config,
            [c for c in records if c["status"] in {"recorded", "pending"}],
        )
        dag_phases, dag_phases_error = self._dag_phases(branch)
        return {
            "root": str(self.root),
            "work_branch": branch,
            "base_branch": self.config.get("base_branch"),
            "main_branch": branch,
            "target_branch": branch,
            "campaign": campaign.get("name") or campaign["key"],
            "campaign_key": key,
            "worktree_branch": self.config.get("work_branch"),
            "dag_path": str(_campaign_dag_path(self.root, branch)) if branch else None,
            "default_branch": self.config.get("default_branch") or integrate.found_default_branch(self.root),
            "units": units,
            "records": records,
            "phases": [w.to_dict() for w in phases],
            "dag_phases": dag_phases,
            "dag_phases_error": dag_phases_error,
            "ready": self.ready_nodes(),
            "paused": self.paused(),
            "dag_merge": dag_merge,
            "checks": self.store.verdict_counts(campaign=key),
            "sandbox": self.sandbox_info(),
        }

    def status_summary(self) -> dict[str, Any]:
        """The compact, human-first status projection (docs/observability.md).

        Mirrors the pi coordinator's ``summarise``: a header, one line per node
        in DAG order, and one line per phase.  The default human ``status``
        output; ``status --verbose`` still returns the full nested dump.  It
        never raises: a plane with no single campaign falls back to the plane
        summary.
        """
        # Normalization is required, so it runs before every projection.
        try:
            self.normalize_dag()
        except SlicemeError:
            pass
        if self._campaign_ref is None and len(self.store.list_campaigns(state="working")) > 1:
            return self._status_summary_plane()
        campaign_row = self.campaign
        if campaign_row is None:
            return self._status_summary_plane()

        branch = self.config.get("work_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        status_state = self._node_states(branch) if branch else {}
        nodes = [n for n in (dag or {}).get("nodes") or [] if n.get("id")]
        node_ids = [str(n["id"]) for n in nodes]

        # The current-phase projection: the scheduler's DAG phases with recorded
        # nodes marked done.
        dag_phases = self._phase_rows(branch) if branch else []
        phase_of: dict[str, int] = {
            member: entry["phase"]
            for entry in dag_phases
            for member in entry["members"]
        }

        name = (dag or {}).get("campaign") or campaign_row.get("name") or "(unnamed)"
        design = (dag or {}).get("design")
        base = (dag or {}).get("base") or self.config.get("base_branch")
        worktree = campaign_row.get("worktree")
        phase_size = (dag or {}).get("concurrency")
        if phase_size is None:
            phase_size = "?"

        lines = [
            f"campaign:    {name}",
            f"work branch: {branch or '(unset)'}  worktree: {worktree or '(unset)'}  base: {base or '(unset)'}",
            f"design:      {design or '(unspecified)'}",
            f"nodes:       {len(node_ids)}  phase size: {phase_size}",
        ]
        node_rows: list[dict[str, Any]] = []
        for node in nodes:
            node_id = str(node["id"])
            phase = phase_of.get(node_id)
            if phase is None:
                phase = "?"
            status = campaign.node_status(status_state, node_id)
            label = node.get("label")
            suffix = f" — {label}" if label else ""
            lines.append(f"  w{phase} {node_id} {status}{suffix}")
            node_rows.append(
                {
                    "id": node_id,
                    "phase": phase,
                    "status": status,
                    "label": label,
                }
            )
        for entry in dag_phases:
            lines.append(
                f"phase {entry['phase']} [{entry['status']}]: {', '.join(entry['members'])}"
            )

        return {
            "root": str(self.root),
            "campaign": name,
            "work_branch": branch,
            "base_branch": base,
            "target_branch": branch,
            "worktree_branch": branch,
            "worktree": worktree,
            "base": base,
            "design": design,
            "node_count": len(node_ids),
            "phase_size": phase_size,
            "nodes": node_rows,
            "dag_phases": dag_phases,
            "paused": self.paused(),
            "lines": lines,
        }

    def _status_summary_plane(self) -> dict[str, Any]:
        """The compact plane projection when no single campaign is bound."""
        plane = self._plane_status()
        lines = [f"campaigns: {len(plane['campaigns'])}"]
        for row in plane["campaigns"]:
            lines.append(
                f"  {row['key']} [{row['state']}] "
                f"work branch: {row['work_branch'] or '(unset)'} "
                f"worktree: {row['worktree'] or '(unset)'}"
            )
        return {**plane, "lines": lines}

    def _plane_status(self) -> dict[str, Any]:
        """A plane-level summary when no campaign is named."""
        cfg = self.plane_config
        campaigns = []
        for row in self.store.list_campaigns():
            records = self.store.list_records(campaign=row["key"])
            campaigns.append(
                {
                    "key": row["key"],
                    "work_branch": row["work_branch"],
                    "target_branch": row["work_branch"],
                    "worktree_branch": row["work_branch"],
                    "worktree": row.get("worktree"),
                    "unit_name": row["unit_name"],
                    "state": row["state"],
                    "delivery_mode": row.get("delivery_mode"),
                    "records": len(records),
                    "delivered": sum(1 for c in records if c["status"] == "delivered"),
                }
            )
        return {
            "root": str(self.root),
            "plane": True,
            "default_branch": cfg.get("default_branch")
            or integrate.found_default_branch(self.root),
            "campaigns": campaigns,
        }

    def normalize_dag(self) -> dict[str, Any]:
        """Contract same-ownership DAG chains before the phase projection.

        This runs before every projection, so a campaign never schedules an
        un-normalized DAG.  A node with recorded progress gets ``no_merge``.
        When that node shares its directory with another node, the merge is
        impossible and the call raises ``revisited_directory``.
        """
        branch = self.config.get("work_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        if not dag or not dag.get("nodes"):
            empty: dict[str, Any] = {"merged": {}}
            if branch:
                empty["path"] = str(campaign.dag_path(self.root, branch))
            return empty

        # Keep any node that already has progress out of the merge.
        nodes = [dict(node) for node in dag["nodes"]]
        statuses = self._descriptor_nodes(branch)
        recorded = {
            str(c["node"])
            for c in self.store.list_records(campaign=self.campaign_key())
            if c.get("node")
        }
        # A re-pass must not rewrite a recorded node.  The engine rejects a plan
        # that removed one, because its commit and its phase would be lost.
        dag_ids = {str(node.get("id")) for node in nodes if node.get("id")}
        missing = sorted(recorded - dag_ids)
        if missing:
            error = SlicemeError(
                "revisited_directory: the plan removed recorded node(s) "
                + ", ".join(missing)
                + "; a re-pass must not rewrite a recorded node"
            )
            error.reason = "revisited_directory"
            raise error
        for node in nodes:
            nid = str(node.get("id"))
            progressed = statuses.get(nid) not in (None, "pending")
            if nid in recorded or progressed:
                node["no_merge"] = True

        phase_size = int(dag.get("concurrency") or DEFAULT_PHASE_SIZE)
        before_phases = plan_dag_phases(list(dag["nodes"]), phase_size=phase_size)
        merged_nodes, merge_map = merge_same_own_nodes(nodes)
        after_phases = plan_dag_phases(merged_nodes, phase_size=phase_size)
        if merge_map:
            write_json(
                campaign.dag_path(self.root, branch), {**dag, "nodes": merged_nodes}
            )
        return {
            "path": str(campaign.dag_path(self.root, branch)),
            "merged": merge_map,
            "before_nodes": len(nodes),
            "after_nodes": len(merged_nodes),
            "before_phases": len(before_phases),
            "after_phases": len(after_phases),
            "phases": [phase.to_dict() for phase in after_phases],
        }

    def _dag_phases(self, branch: str | None) -> tuple[list[dict[str, Any]], str | None]:
        """Compute the scheduler's phase projection of the campaign DAG.

        The DAG is the only authored schedule; phases are derived here from
        ``dir``/``depends_on`` and the campaign's ``concurrency`` cap.  A
        malformed or cyclic DAG is reported rather than crashing ``status``.
        """
        if not branch:
            return [], None
        dag = campaign.load_dag(self.root, branch)
        if not dag or not dag.get("nodes"):
            return [], None
        phase_size = int(dag.get("concurrency") or DEFAULT_PHASE_SIZE)
        try:
            validate_dag(list(dag["nodes"]))
            planned = plan_dag_phases(list(dag["nodes"]), phase_size=phase_size)
        except SlicemeError as exc:
            return [], str(exc)
        return [w.to_dict() for w in planned], None

    def _project_campaign(self, row: dict[str, Any]) -> dict[str, Any]:
        """The dashboard projection of the campaign's one worktree writer.

        The campaign owns the worktree and the branch, so the projection reads
        the campaign row rather than a retired unit row.  ``record`` and
        ``verification`` are the campaign's newest committed record and its
        check; an empty marker is skipped because its stored head is another
        node's commit.
        """
        key = str(row["key"])
        records = self.store.list_records(campaign=key)
        latest = next((r for r in reversed(records) if not r.get("empty")), None)
        verification = (
            self.store.latest_verdict_for_commit(str(latest["head_commit"]))
            if latest is not None
            else None
        )
        node = str(latest["node"]) if latest is not None and latest.get("node") else None
        return {
            "id": row.get("id"),
            "name": row.get("unit_name") or key,
            "kind": "campaign",
            "worktree": row.get("worktree"),
            "branch": row.get("work_branch"),
            "state": row.get("state"),
            "delivery_mode": row.get("delivery_mode"),
            "merge_commit": row.get("merge_commit"),
            "node": node,
            "log": str(campaign.worker_log_path(self.root, row["work_branch"], node))
            if node
            else None,
            "record": int(latest["id"]) if latest is not None else None,
            "verification": verification,
        }

    def campaign_plan(self, design: str | Path) -> dict[str, Any]:
        """The design's campaign split joined with the registry state.

        The entries keep the design order.  ``next`` names the first entry whose
        campaign is not delivered, landed, or closed.  The caller runs the
        entries in order.  A directory may repeat across entries: the engine
        keeps one writer per directory inside one campaign, and the campaign
        boundary lets the next campaign own the same directory.
        """
        from .. import plan as plan_mod

        entries = plan_mod.load_campaign_plan(self.root, design)
        rows = self.store.list_campaigns()
        result: list[dict[str, Any]] = []
        next_name: str | None = None
        for index, entry in enumerate(entries):
            row = next(
                (
                    item
                    for item in rows
                    if (item.get("name") or "") == entry["name"]
                    or item["work_branch"] == entry["target"]
                ),
                None,
            )
            state = str(row["state"]) if row else "pending"
            result.append(
                {
                    "index": index,
                    "name": entry["name"],
                    "target": entry["target"],
                    "base": self.plane_config.get("base_branch")
                    or self.plane_config.get("base"),
                    "dirs": entry["dirs"],
                    "state": state,
                    "campaign_key": row["key"] if row else None,
                }
            )
            if next_name is None and state not in ("delivered", "landed", "closed"):
                next_name = entry["name"]
        return {
            "design": str(design),
            "entries": result,
            "next": next_name,
            "total": len(result),
        }
