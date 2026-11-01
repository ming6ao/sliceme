"""Session and resume verbs for the sliceme engine.

Internal module: :class:`sliceme.service.Service` composes this mixin and
:func:`sliceme.surface.dispatch` stays the one verb facade.
"""

from __future__ import annotations

from typing import Any

from .. import campaign, gitutil
from ..util import (
    SlicemeError,
)


class SessionVerbs:

    def resume(self) -> dict[str, Any]:
        """Reconcile a suspended campaign and return its resume plan.

        Git and ``state.db`` always win over the adapter-written descriptor.
        The plan is a pure computation over git plus ``state.db``, so it writes
        nothing.
        """
        config = self.config
        campaign_row = self.campaign
        branch = (
            (campaign_row or {}).get("work_branch")
            or config.get("work_branch")
            or "main"
        )
        descriptor = campaign.load_session(self.root, branch) or {}
        descriptor_nodes = descriptor.get("nodes") or {}
        dag = campaign.load_dag(self.root, branch) or {}
        node_ids = [str(n["id"]) for n in dag.get("nodes", []) if n.get("id")]
        if not node_ids:
            node_ids = sorted(str(key) for key in descriptor_nodes)

        latest: dict[str, dict[str, Any]] = {}
        for record in self.store.list_records(campaign=self.campaign_key()):
            node = record.get("node")
            if node:
                latest[str(node)] = record  # ascending id order: last wins

        # The campaign owns the one worktree, so resume reads it from the row.
        worktree = self.campaign_worktree()
        worktree_present = bool(worktree and worktree.exists())
        worktree_dirty = bool(worktree_present and not gitutil.is_clean(worktree))

        current_phase = self._phase_cursor(branch).index

        plan: dict[str, Any] = {
            "record_phase": None,
            "resume": [],
            "respawn": [],
            "verify": [],
            "blocked": [],
        }
        statuses: dict[str, str] = {}
        tries: dict[str, int] = {}
        stalls: dict[str, int] = {}
        for node in node_ids:
            entry = self._descriptor_entry(descriptor_nodes, node)
            recorded_commit = entry.get("commit")
            record = latest.get(node)
            if (
                not recorded_commit
                and record is not None
                and not record.get("empty")
            ):
                recorded_commit = record.get("head_commit")
            status = self._resume_status(
                entry, record, recorded_commit, worktree_present
            )
            statuses[node] = status
            # The per-node counts a resumed campaign restarts from: the try
            # count comes from the records (or 1 when none), the stall count
            # from the descriptor the budget tick kept.
            try:
                tries[node] = max(1, int((record or {}).get("tries") or 1))
            except (TypeError, ValueError):
                tries[node] = 1
            stalls[node] = int(entry.get("stalls") or 0)
            if status == "recorded":
                # A delivered node is finished; an undelivered one re-verifies
                # from the check cache.
                if record is not None and record.get("status") == "delivered":
                    continue
                plan["verify"].append(node)
            elif status == "running":
                plan["resume"].append(node)
            elif status == "pending" and (
                record is not None or int(entry.get("attempts") or 0) > 0
            ):
                plan["respawn"].append(node)
        if plan["resume"] or worktree_dirty:
            plan["record_phase"] = current_phase

        result = {
            "campaign": descriptor.get("campaign") or dag.get("campaign"),
            "work_branch": branch,
            "worktree_branch": config.get("work_branch"),
            "worktree": str(worktree) if worktree else None,
            "worktree_present": worktree_present,
            "worktree_dirty": worktree_dirty,
            "current_phase": current_phase,
            "nodes": statuses,
            "tries": tries,
            "stalls": stalls,
            "resume_plan": plan,
            "descriptor": descriptor or None,
        }
        return result

    @staticmethod
    def _descriptor_entry(nodes: dict[str, Any], node: str) -> dict[str, Any]:
        """One descriptor node entry as an object (a bare status string is
        accepted)."""
        entry = nodes.get(node)
        if isinstance(entry, dict):
            return entry
        if isinstance(entry, str) and entry:
            return {"status": entry}
        return {}

    @staticmethod
    def _resume_status(
        entry: dict[str, Any],
        record: dict[str, Any] | None,
        recorded_commit: Any,
        worktree_present: bool,
    ) -> str:
        """Map one node to its resume status from plane evidence.

        The vocabulary is ``pending``, ``running``, ``recorded``, and
        ``failed``.  The commit comparison is essential: a verified-but-
        undelivered node is ``recorded`` while its record is still
        ``recorded``, so a status-only rule would wrongly re-run it.
        """
        record_status = record.get("status") if record else None
        # An empty marker means the node recorded zero changes.  It carries no
        # commit, so it must not borrow a sibling's head to look recorded.
        if record is not None and record.get("empty"):
            return "recorded"
        head = record.get("head_commit") if record else None
        if record_status == "delivered":
            return "recorded"
        if entry.get("status") in {"done", "recorded"}:
            if not record:
                return "recorded"
            if recorded_commit and head == recorded_commit:
                return "recorded"
        if record_status == "recorded":
            if recorded_commit and head == recorded_commit:
                return "recorded"
            return "pending"
        if entry.get("status") in {"running", "paused"}:
            return "running" if worktree_present else "pending"
        return "pending"

    def sessions(self) -> dict[str, Any]:
        """List every registered campaign from its descriptor files."""
        branch = (
            self.plane_config.get("work_branch")
            or self.plane_config.get("main_branch")
            or "main"
        )
        current = None
        try:
            current = self.campaign
        except SlicemeError:
            current = None
        if current is not None:
            branch = current["work_branch"]
        entries: list[dict[str, Any]] = []
        for key, descriptor in campaign.list_sessions(self.root):
            work_branch = str(descriptor.get("feature_branch") or key)
            row = self.store.get_campaign(work_branch)
            campaign_key = row["key"] if row else None
            recorded = (
                {
                    str(record["node"])
                    for record in self.store.list_records(campaign=campaign_key)
                    if record.get("node") and not record.get("empty")
                }
                if campaign_key
                else set()
            )
            dag = campaign.load_dag(self.root, work_branch) or {}
            total = len([n for n in dag.get("nodes", []) if n.get("id")])
            done = len(recorded)
            entries.append(
                {
                    "work_branch": work_branch,
                    "is_current": work_branch == branch,
                    "campaign": descriptor.get("campaign") or dag.get("campaign"),
                    "label": descriptor.get("label"),
                    "status": descriptor.get("status") or "suspended",
                    "reason": descriptor.get("reason"),
                    "phase": descriptor.get("current_phase"),
                    "done": done,
                    "total": total,
                    "suspended_at": descriptor.get("suspended_at"),
                    "session_file": (descriptor.get("pi") or {}).get("session_file"),
                    "descriptor": str(campaign.session_path(self.root, work_branch)),
                }
            )
        return {"root": str(self.root), "sessions": entries}
