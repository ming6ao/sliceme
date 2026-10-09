"""Status, ready, and plan verbs for the sliceme engine.

Internal module: :class:`sliceme.service.Service` composes this mixin and
:func:`sliceme.surface.dispatch` stays the one verb facade.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from .. import campaign, integrate
from ..ownership import (
    DEFAULT_WAVE_SIZE,
    merge_same_own_nodes,
    plan_dag_waves,
    validate_dag,
)
from ..util import (
    SlicemeError,
    write_json,
)


class WaveCursor(NamedTuple):
    """The one wave cursor that drives ``ready``, ``record``, and ``check``.

    ``index``/``members`` name the wave to work on: the first planned wave that
    is not fully recorded, or the last wave when every wave is recorded.  A
    member that recorded zero changes still counts as recorded (an empty
    record), so the cursor advances past a no-op wave instead of stalling.

    ``recorded_index``/``recorded_members`` name the wave ``check`` runs: the
    wave the newest record belongs to, scoped to the members with a real
    commit.  ``record`` commits the current wave and ``check`` runs right after
    it, so check reads the wave just recorded, not the next one.

    ``recorded_planned_members`` is that wave's full planned membership.  The
    GPU tier still reflects the wave when every member recorded empty, even
    though the empty members contribute no acceptance command.
    """

    index: int | None
    members: list[str]
    recorded_index: int | None
    recorded_members: list[str]
    recorded_planned_members: list[str]


class StatusVerbs:

    def ready_nodes(self) -> list[str]:
        """The ids of the current wave's nodes ready to spawn.

        A wave is the one scheduling gate: every dependency lives in an earlier
        wave (``plan_dag_waves`` sets ``min_wave = max(wave(dep) + 1)``), so a
        member is ready once its own status is neither finished nor running.
        A missing DAG has no ready nodes.
        """
        branch = self.config.get("work_branch")
        if not branch:
            return []
        return self._ready_in_current_wave(branch)

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
        evidence that a wave member finished, so the wave cursor advances.
        """
        return {
            str(record["node"])
            for record in self.store.list_records(campaign=self.campaign_key())
            if record.get("node")
        }

    def _committed_nodes(self) -> set[str]:
        """The node ids with a real commit (a record that changed files).

        An explicit empty record is not a commit, so it never contributes its
        acceptance commands to the wave check vector.
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
        """The node ids the wave cursor treats as done: the recorded nodes."""
        return set(self._recorded_nodes())

    def _node_states(self, branch: str) -> dict[str, Any]:
        """The per-node status for display: recorded nodes are ``recorded``.

        The node vocabulary is ``pending``, ``running``, ``recorded``, and
        ``failed``.  The wave verdict carries pass/fail, so a recorded node is
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

    def _wave_rows(self, branch: str) -> list[dict[str, Any]]:
        """The wave projection for the ``status_summary`` reads.

        The scheduler's DAG waves are projected, and a wave is ``done`` when
        every member is finished, so the engine advances without a state file.
        """
        planned, _error = self._dag_waves(branch)
        finished = self._finished_nodes()
        rows = []
        for entry in planned:
            members = [str(m) for m in entry.get("members") or []]
            status = "done" if members and set(members) <= finished else "pending"
            rows.append(
                {"wave": int(entry.get("wave", 0)), "status": status, "members": members}
            )
        return rows

    def _wave_cursor(self, branch: str) -> WaveCursor:
        """The one cursor for ``ready``, ``record``, and ``check``.

        The DAG projection plus the records in SQLite derive both positions:
        the wave to work on (first not fully recorded, else the last) and the
        wave ``check`` runs (the wave of the newest record, scoped to the
        members with a real commit).  See :class:`WaveCursor`.
        """
        planned, _error = self._dag_waves(branch)
        finished = self._finished_nodes()
        committed = self._committed_nodes()
        index = next(
            (
                int(entry.get("wave", 0))
                for entry in planned
                if not set(str(m) for m in entry.get("members") or []) <= finished
            ),
            None,
        )
        if index is None and planned:
            index = int(planned[-1].get("wave", 0))
        members = next(
            (
                [str(m) for m in entry.get("members") or []]
                for entry in planned
                if int(entry.get("wave", 0)) == index
            ),
            [],
        )
        recorded_index, recorded_members, recorded_planned = self._recorded_wave_of(
            planned, committed
        )
        return WaveCursor(
            index, members, recorded_index, recorded_members, recorded_planned
        )

    def _recorded_wave_of(
        self, planned: list[dict[str, Any]], committed: set[str]
    ) -> tuple[int | None, list[str], list[str]]:
        """The wave ``check`` runs: the wave of the newest record.

        ``record`` commits the finished nodes, then ``check`` runs against that
        same tree, so check names the wave the record just committed, not the
        next wave ``ready`` offers.  The newest record selects the wave; the
        wave's committed members form the acceptance vector, so an empty record
        contributes nothing.  When that wave has no committed member at all the
        vector is empty.  The planned members are the fallback only when nothing
        is recorded yet, and the first planned wave when the DAG has no records.
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
                    wave_members = [m for m in members if m in committed]
                    return int(entry.get("wave", 0)), wave_members, members
        if not planned:
            return None, [], []
        first = planned[0]
        first_members = [str(m) for m in first.get("members") or []]
        return int(first.get("wave", 0)), first_members, first_members

    def _ready_in_current_wave(self, branch: str) -> list[str]:
        """The current wave's members that are neither finished nor running."""
        cursor = self._wave_cursor(branch)
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
        """The current wave's ready node ids, its index, and the pause flag.

        The wave is the one scheduling gate: a node is ready when it is a
        member of the current wave and its own status is neither finished nor
        running.  The result is scoped to the current wave, so a caller spawns
        and records one wave per call.  A missing or malformed DAG reports no
        ready nodes instead of crashing the read.
        """
        row = self.campaign
        if row is None:
            return {"campaign": None, "ready": [], "wave": None, "paused": False}
        branch = str(row["work_branch"])
        cursor = self._wave_cursor(branch)
        return {
            "campaign": row.get("name") or row["key"],
            "ready": self._ready_in_current_wave(branch),
            "wave": cursor.index,
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
        waves = integrate.plan_waves(
            self.store,
            self.root,
            self.config,
            [c for c in records if c["status"] in {"recorded", "pending"}],
        )
        dag_waves, dag_waves_error = self._dag_waves(branch)
        return {
            "root": str(self.root),
            "work_branch": branch,
            "base_branch": self.config.get("base_branch"),
            "main_branch": branch,
            "target_branch": branch,
            "campaign": campaign.get("name") or campaign["key"],
            "campaign_key": key,
            "worktree_branch": self.config.get("work_branch"),
            "feature_branch": branch,
            "default_branch": self.config.get("default_branch") or integrate.found_default_branch(self.root),
            "units": units,
            "records": records,
            "waves": [w.to_dict() for w in waves],
            "dag_waves": dag_waves,
            "dag_waves_error": dag_waves_error,
            "ready": self.ready_nodes(),
            "paused": self.paused(),
            "dag_merge": dag_merge,
            "checks": self.store.verdict_counts(campaign=key),
            "sandbox": self.sandbox_info(),
        }

    def status_summary(self) -> dict[str, Any]:
        """The compact, human-first status projection (docs/observability.md).

        Mirrors the pi coordinator's ``summarise``: a header, one line per node
        in DAG order, and one line per wave.  The default human ``status``
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

        # The current-wave projection: the scheduler's DAG waves with recorded
        # nodes marked done.
        dag_waves = self._wave_rows(branch) if branch else []
        wave_of: dict[str, int] = {
            member: entry["wave"]
            for entry in dag_waves
            for member in entry["members"]
        }

        name = (dag or {}).get("campaign") or campaign_row.get("name") or "(unnamed)"
        design = (dag or {}).get("design")
        base = (dag or {}).get("base")
        worktree = self.config.get("work_branch")
        wave_size = (dag or {}).get("concurrency")
        if wave_size is None:
            wave_size = "?"

        lines = [
            f"campaign: {name}",
            f"target:   {branch or '(unset)'}  worktree: {worktree or '(unset)'}  base: {base or '(unset)'}",
            f"design:   {design or '(unspecified)'}",
            f"nodes:    {len(node_ids)}  wave size: {wave_size}",
        ]
        node_rows: list[dict[str, Any]] = []
        for node in nodes:
            node_id = str(node["id"])
            wave = wave_of.get(node_id)
            if wave is None:
                wave = "?"
            status = campaign.node_status(status_state, node_id)
            label = node.get("label")
            suffix = f" — {label}" if label else ""
            lines.append(f"  w{wave} {node_id} {status}{suffix}")
            node_rows.append(
                {
                    "id": node_id,
                    "wave": wave,
                    "status": status,
                    "label": label,
                }
            )
        for entry in dag_waves:
            lines.append(
                f"wave {entry['wave']} [{entry['status']}]: {', '.join(entry['members'])}"
            )

        return {
            "root": str(self.root),
            "campaign": name,
            "work_branch": branch,
            "base_branch": base,
            "target_branch": branch,
            "worktree_branch": worktree,
            "base": base,
            "design": design,
            "node_count": len(node_ids),
            "wave_size": wave_size,
            "nodes": node_rows,
            "dag_waves": dag_waves,
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
                f"target: {row['work_branch'] or '(unset)'} "
                f"worktree: {row['work_branch'] or '(unset)'}"
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
                    "unit_name": row["unit_name"],
                    "state": row["state"],
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
        """Contract same-ownership DAG chains before the wave projection.

        This runs before every projection, so a campaign never schedules an
        un-normalized DAG.  A node with recorded progress stays separate.
        """
        branch = self.config.get("work_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        if not dag or not dag.get("nodes"):
            return {"merged": {}}

        # Keep any node that already has progress out of the merge.
        nodes = [dict(node) for node in dag["nodes"]]
        statuses = self._descriptor_nodes(branch)
        recorded = {
            str(c["node"])
            for c in self.store.list_records(campaign=self.campaign_key())
            if c.get("node")
        }
        for node in nodes:
            nid = str(node.get("id"))
            progressed = statuses.get(nid) not in (None, "pending")
            if nid in recorded or progressed:
                node["no_merge"] = True

        wave_size = int(dag.get("concurrency") or DEFAULT_WAVE_SIZE)
        before_waves = plan_dag_waves(list(dag["nodes"]), wave_size=wave_size)
        merged_nodes, merge_map = merge_same_own_nodes(nodes)
        after_waves = plan_dag_waves(merged_nodes, wave_size=wave_size)
        if merge_map:
            write_json(
                campaign.dag_path(self.root, branch), {**dag, "nodes": merged_nodes}
            )
        return {
            "path": str(campaign.dag_path(self.root, branch)),
            "merged": merge_map,
            "before_nodes": len(nodes),
            "after_nodes": len(merged_nodes),
            "before_waves": len(before_waves),
            "after_waves": len(after_waves),
            "waves": [wave.to_dict() for wave in after_waves],
        }

    def _dag_waves(self, branch: str | None) -> tuple[list[dict[str, Any]], str | None]:
        """Compute the scheduler's wave projection of the campaign DAG.

        The DAG is the only authored schedule; waves are derived here from
        ``owns``/``depends_on`` and the campaign's ``concurrency`` cap.  A
        malformed or cyclic DAG is reported rather than crashing ``status``.
        """
        if not branch:
            return [], None
        dag = campaign.load_dag(self.root, branch)
        if not dag or not dag.get("nodes"):
            return [], None
        wave_size = int(dag.get("concurrency") or DEFAULT_WAVE_SIZE)
        try:
            validate_dag(list(dag["nodes"]))
            planned = plan_dag_waves(list(dag["nodes"]), wave_size=wave_size)
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
