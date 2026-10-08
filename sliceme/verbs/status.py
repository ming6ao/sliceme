"""Status, ready, and plan verbs for the sliceme engine.

Internal module: :class:`sliceme.service.Service` composes this mixin and
:func:`sliceme.surface.dispatch` stays the one verb facade.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import campaign, integrate
from ..ownership import (
    DEFAULT_WAVE_SIZE,
    merge_same_own_nodes,
    plan_dag_waves,
    readiness,
    validate_dag,
)
from ..util import (
    SlicemeError,
    write_json,
)


class StatusVerbs:

    def ready_nodes(self) -> list[str]:
        """The ids of the DAG nodes ready to spawn (readiness is the gate).

        A node is ready when every ``depends_on`` dependency is ``done`` and the
        node itself is neither ``done`` nor ``running``.  Waves stay a display
        hint (DEC-2): the spawn gate is readiness, not wave membership.  A
        missing DAG has no ready nodes; a malformed DAG reports none rather than
        crashing a status read (the error stays visible in ``dag_waves_error``).
        """
        branch = self.config.get("target_branch") or self.config.get("main_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        if not dag or not dag.get("nodes"):
            return []
        state = self._readiness_state(branch) if branch else {"nodes": {}}
        try:
            return readiness(list(dag["nodes"]), state)
        except SlicemeError:
            return []

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
        control = campaign.load_control(self.root, str(row["target_branch"]))
        return bool(control and control.get("pause"))

    def _recorded_nodes(self) -> set[str]:
        """The node ids with a recorded candidate (the durable ``done`` signal).

        The engine no longer writes ``state.json``, so a recorded candidate is
        the only durable evidence that a node produced its commit.
        """
        return {
            str(candidate["node"])
            for candidate in self.store.list_candidates(campaign=self.campaign_key())
            if candidate.get("node")
        }

    def _readiness_state(self, branch: str) -> dict[str, Any]:
        """The readiness state: ``state.json`` nodes plus recorded candidates.

        A node with a recorded candidate is ``done``.  The adapter-written
        ``state.json`` still wins for a node it describes, so a running or an
        explicitly done node keeps its status.
        """
        state = campaign.load_state(self.root, branch)
        entries = state.get("nodes")
        merged = dict(entries) if isinstance(entries, dict) else {}
        for node in self._recorded_nodes():
            entry = merged.get(node)
            if not isinstance(entry, dict):
                entry = {}
            merged[node] = {**entry, "status": "done"}
        return {**state, "nodes": merged}

    def _wave_rows(self, branch: str) -> list[dict[str, Any]]:
        """The wave projection for the current-wave and summary reads.

        ``state.json`` wins when the adapter wrote wave state.  Otherwise the
        scheduler's DAG waves are projected, and a wave is ``done`` when every
        member has a recorded candidate, so the engine advances without a
        ``state.json`` writer.  An entry may spell the index ``index``
        (adapter state) or ``wave`` (the engine projection).
        """
        state = campaign.load_state(self.root, branch)
        state_waves = state.get("waves")
        if isinstance(state_waves, list) and state_waves:
            rows: list[dict[str, Any]] = []
            for entry in state_waves:
                if not isinstance(entry, dict):
                    continue
                number = entry.get("index")
                if number is None:
                    number = entry.get("wave")
                rows.append(
                    {
                        "wave": int(number) if number is not None else 0,
                        "status": entry.get("status"),
                        "members": [str(m) for m in entry.get("members") or []],
                    }
                )
            return rows
        planned, _error = self._dag_waves(branch)
        recorded = self._recorded_nodes()
        rows = []
        for entry in planned:
            members = [str(m) for m in entry.get("members") or []]
            status = "done" if members and set(members) <= recorded else "pending"
            rows.append(
                {"wave": int(entry.get("wave", 0)), "status": status, "members": members}
            )
        return rows

    def _current_wave(self, branch: str) -> tuple[int | None, list[str]]:
        """Resolve the current wave index and its members.

        ``state.json`` wins when it carries an explicit ``current_wave`` or a
        ``waves`` list.  Otherwise the current wave is the first planned DAG
        wave that is not fully recorded, derived from SQLite.  When every wave
        is recorded the last wave stays current, so a lone recorded wave keeps
        serving ``check --current``.
        """
        state = campaign.load_state(self.root, branch)
        rows = self._wave_rows(branch)
        explicit = state.get("current_wave")
        index: int | None = int(explicit) if explicit is not None else None
        if index is None:
            index = next(
                (int(row["wave"]) for row in rows if str(row.get("status")) != "done"),
                None,
            )
        if index is None and rows:
            index = int(rows[-1]["wave"])
        members = next(
            (
                [str(m) for m in row["members"]]
                for row in rows
                if int(row["wave"]) == index
            ),
            [],
        )
        return index, members

    def _recorded_wave(self, branch: str) -> tuple[int | None, list[str]]:
        """Resolve the planned wave that the newest recorded candidate is in.

        ``wave --record --current`` commits the finished nodes, then
        ``check --current`` runs against that same tree.  So ``check --current``
        must name the wave the record just committed, not the next wave that
        ``ready`` offers.  The newest recorded candidate (maximum id) selects
        the wave, and the wave's members are its recorded candidates, so a
        member that recorded no candidate does not contribute its acceptance
        to the check vector.  The planned members are the fallback when that
        wave has no candidate, and the first planned wave is the fallback when
        nothing is recorded yet.
        """
        planned, _error = self._dag_waves(branch)
        recorded = self._recorded_nodes()
        newest: str | None = None
        for candidate in self.store.list_candidates(campaign=self.campaign_key()):
            node = candidate.get("node")
            if node:
                newest = str(node)  # ascending id order: last wins
        if newest is not None:
            for entry in planned:
                members = [str(m) for m in entry.get("members") or []]
                if newest in members:
                    wave_members = [m for m in members if m in recorded]
                    return int(entry.get("wave", 0)), (wave_members or members)
        if not planned:
            return None, []
        first = planned[0]
        return int(first.get("wave", 0)), [str(m) for m in first.get("members") or []]

    def ready(self) -> dict[str, Any]:
        """The current wave's ready node ids, its index, and the pause flag.

        Readiness is the spawn gate: a node is ready when every ``depends_on``
        dependency is ``done`` and the node is neither ``done`` nor ``running``.
        The result is scoped to the current wave, so a caller spawns and records
        one wave per call.  A missing or malformed DAG reports no ready nodes
        instead of crashing the read.
        """
        row = self.campaign
        if row is None:
            return {"campaign": None, "ready": [], "wave": None, "paused": False}
        branch = str(row["target_branch"])
        index, members = self._current_wave(branch)
        ready: list[str] = []
        dag = campaign.load_dag(self.root, branch)
        nodes = (dag or {}).get("nodes") or []
        if nodes and members:
            state = self._readiness_state(branch)
            try:
                ready = readiness(list(nodes), state)
            except SlicemeError:
                ready = []
            in_wave = set(members)
            ready = [nid for nid in ready if nid in in_wave]
        return {
            "campaign": row.get("name") or row["key"],
            "ready": ready,
            "wave": index,
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
        branch = self.config.get("target_branch") or self.config.get("main_branch")
        # Normalization is required, so it runs before every projection.
        try:
            dag_merge = self.normalize_dag()
        except SlicemeError as exc:
            dag_merge = {"merged": {}, "error": str(exc)}
        units = [self._project_unit(u, branch) for u in self._scoped_units()]
        candidates = self.store.list_candidates(campaign=key)
        waves = integrate.plan_waves(
            self.store,
            self.root,
            self.config,
            [c for c in candidates if c["status"] in {"prepared", "pending"}],
        )
        dag_waves, dag_waves_error = self._dag_waves(branch)
        return {
            "root": str(self.root),
            "main_branch": branch,
            "target_branch": branch,
            "campaign": campaign.get("name") or campaign["key"],
            "campaign_key": key,
            "worktree_branch": self.config.get("worktree_branch"),
            "feature_branch": branch,
            "default_branch": self.config.get("default_branch") or integrate.found_default_branch(self.root),
            "units": units,
            "candidates": candidates,
            "waves": [w.to_dict() for w in waves],
            "dag_waves": dag_waves,
            "dag_waves_error": dag_waves_error,
            "ready": self.ready_nodes(),
            "paused": self.paused(),
            "dag_merge": dag_merge,
            "checks": self.store.check_counts(campaign=key),
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

        branch = self.config.get("target_branch") or self.config.get("main_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        state = campaign.load_state(self.root, branch) if branch else {"nodes": {}}
        status_state = self._readiness_state(branch) if branch else state
        nodes = [n for n in (dag or {}).get("nodes") or [] if n.get("id")]
        node_ids = [str(n["id"]) for n in nodes]

        # The current-wave projection: adapter wave state when present, else the
        # scheduler's DAG waves with recorded nodes marked done.
        dag_waves = self._wave_rows(branch) if branch else []
        wave_of: dict[str, int] = {
            member: entry["wave"]
            for entry in dag_waves
            for member in entry["members"]
        }

        name = (dag or {}).get("campaign") or campaign_row.get("name") or "(unnamed)"
        design = (dag or {}).get("design")
        base = (dag or {}).get("base") or state.get("base")
        worktree = self.config.get("worktree_branch")
        wave_size = state.get("wave_size")
        if wave_size is None and dag:
            wave_size = dag.get("concurrency")
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
            entry = (state.get("nodes") or {}).get(node_id) or {}
            wave = wave_of.get(node_id)
            if wave is None and isinstance(entry, dict):
                wave = entry.get("wave")
            if wave is None:
                wave = "?"
            phase = node.get("phase")
            status = campaign.node_status(status_state, node_id)
            label = node.get("label")
            suffix = f" — {label}" if label else ""
            lines.append(f"  w{wave} {node_id} [{phase or '-'}] {status}{suffix}")
            node_rows.append(
                {
                    "id": node_id,
                    "wave": wave,
                    "phase": phase,
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
                f"target: {row['target_branch'] or '(unset)'} "
                f"worktree: {row['worktree_branch'] or '(unset)'}"
            )
        return {**plane, "lines": lines}

    def _plane_status(self) -> dict[str, Any]:
        """A plane-level summary when no campaign is named."""
        cfg = self.plane_config
        campaigns = []
        for row in self.store.list_campaigns():
            candidates = self.store.list_candidates(campaign=row["key"])
            campaigns.append(
                {
                    "key": row["key"],
                    "target_branch": row["target_branch"],
                    "worktree_branch": row["worktree_branch"],
                    "unit_name": row["unit_name"],
                    "state": row["state"],
                    "candidates": len(candidates),
                    "landed": sum(1 for c in candidates if c["status"] == "landed"),
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
        branch = self.config.get("target_branch") or self.config.get("main_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        if not dag or not dag.get("nodes"):
            return {"merged": {}}

        # Keep any node that already has progress out of the merge.
        nodes = [dict(node) for node in dag["nodes"]]
        statuses = campaign.load_state(self.root, branch).get("nodes") or {}
        recorded = {
            str(c["node"])
            for c in self.store.list_candidates(campaign=self.campaign_key())
            if c.get("node")
        }
        for node in nodes:
            nid = str(node.get("id"))
            entry = statuses.get(nid)
            progressed = isinstance(entry, dict) and entry.get("status") not in (
                None,
                "pending",
            )
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

    def _project_unit(self, unit: dict[str, Any], branch: str | None) -> dict[str, Any]:
        """Add the campaign columns the dashboard needs (§6.3).

        ``node``/``log`` come from the campaign layout; ``candidate`` and
        ``verification`` are the unit's latest candidate row and the newest
        check for its commit.
        """
        projected = dict(unit)
        unit_id = int(unit["id"])
        candidates = [
            c
            for c in self.store.list_candidates(campaign=self.campaign_key())
            if int(c["unit_id"]) == unit_id
        ]
        latest = candidates[-1] if candidates else None
        verification = (
            self.store.latest_check_for_commit(str(latest["head_commit"]))
            if latest is not None
            else None
        )
        projected["node"] = unit["name"]
        projected["log"] = str(
            campaign.worker_log_path(self.root, branch or "main", unit["name"])
        )
        projected["candidate"] = int(latest["id"]) if latest is not None else None
        projected["verification"] = verification
        return projected

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
        previous_target: str | None = None
        next_name: str | None = None
        for index, entry in enumerate(entries):
            row = next(
                (
                    item
                    for item in rows
                    if (item.get("name") or "") == entry["name"]
                    or item["target_branch"] == entry["target"]
                ),
                None,
            )
            state = str(row["state"]) if row else "pending"
            result.append(
                {
                    "index": index,
                    "name": entry["name"],
                    "target": entry["target"],
                    "base": entry["base"]
                    or previous_target
                    or self.plane_config.get("base"),
                    "dirs": entry["dirs"],
                    "state": state,
                    "campaign_key": row["key"] if row else None,
                }
            )
            if next_name is None and state not in ("delivered", "landed", "closed"):
                next_name = entry["name"]
            previous_target = entry["target"]
        return {
            "design": str(design),
            "entries": result,
            "next": next_name,
            "total": len(result),
        }
