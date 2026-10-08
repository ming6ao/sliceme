"""Session and resume verbs for the sliceme engine.

Internal module: :class:`sliceme.service.Service` composes this mixin and
:func:`sliceme.surface.dispatch` stays the one verb facade.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import campaign, gitutil
from ..util import (
    SlicemeError,
)


class SessionVerbs:

    def resume(self, *, plan_only: bool = False) -> dict[str, Any]:
        """Reconcile a suspended campaign and return its resume plan.

        Git and ``state.db`` always win over the adapter-written descriptor and
        ``state.json``.  The plan is a pure computation over git plus
        ``state.db``: it writes nothing, so ``plan_only`` is a compatibility
        no-op kept for the CLI surface.
        """
        config = self.config
        branch = (
            config.get("target_branch") or config.get("main_branch") or "main"
        )
        descriptor = campaign.load_session(self.root, branch) or {}
        descriptor_nodes = descriptor.get("nodes") or {}
        state = campaign.load_state(self.root, branch)
        state_nodes: dict[str, Any] = state.get("nodes") or {}
        dag = campaign.load_dag(self.root, branch) or {}
        node_ids = [str(n["id"]) for n in dag.get("nodes", []) if n.get("id")]
        if not node_ids:
            node_ids = sorted(state_nodes)

        latest: dict[str, dict[str, Any]] = {}
        for candidate in self.store.list_candidates(campaign=self.campaign_key()):
            node = candidate.get("node")
            if node:
                latest[str(node)] = candidate  # ascending id order: last wins

        campaign_row = self.campaign
        unit = None
        if campaign_row is not None:
            unit = self.store.get_unit_by_campaign(str(campaign_row["key"]))
        unit = unit or self.store.get_unit("campaign")
        worktree = Path(unit["worktree"]) if unit and unit.get("worktree") else None
        worktree_present = bool(worktree and worktree.exists())
        worktree_dirty = bool(worktree_present and not gitutil.is_clean(worktree))

        current_wave, _members = self._current_wave(branch)

        plan: dict[str, Any] = {
            "record_wave": None,
            "resume": [],
            "respawn": [],
            "verify": [],
            "blocked": [],
        }
        statuses: dict[str, str] = {}
        for node in node_ids:
            entry = state_nodes.get(node) if isinstance(state_nodes.get(node), dict) else {}
            desc_node = (
                descriptor_nodes.get(node)
                if isinstance(descriptor_nodes.get(node), dict)
                else {}
            )
            recorded_commit = entry.get("commit") or desc_node.get("commit")
            candidate = latest.get(node)
            if not recorded_commit and candidate is not None:
                recorded_commit = candidate.get("head_commit")
            status = self._resume_status(
                entry, candidate, recorded_commit, worktree_present
            )
            statuses[node] = status
            if status == "done":
                continue
            if status == "recorded":
                plan["verify"].append(node)
            elif status == "paused":
                plan["resume"].append(node)
            elif status == "pending" and (
                candidate is not None or int(entry.get("attempts") or 0) > 0
            ):
                plan["respawn"].append(node)
        if plan["resume"] or worktree_dirty:
            plan["record_wave"] = current_wave

        result = {
            "campaign": descriptor.get("campaign") or dag.get("campaign"),
            "feature_branch": branch,
            "worktree_branch": config.get("worktree_branch"),
            "worktree": str(worktree) if worktree else None,
            "worktree_present": worktree_present,
            "worktree_dirty": worktree_dirty,
            "current_wave": current_wave,
            "nodes": statuses,
            "resume_plan": plan,
            "descriptor": descriptor or None,
        }
        return result

    @staticmethod
    def _resume_status(
        entry: dict[str, Any],
        candidate: dict[str, Any] | None,
        recorded_commit: Any,
        worktree_present: bool,
    ) -> str:
        """Map one node to its resume status from plane evidence.

        The commit comparison is essential: a verified-but-undelivered node is
        ``done`` in ``state.json`` while its candidate is still ``prepared``,
        so a status-only rule would wrongly re-run it.
        """
        candidate_status = candidate.get("status") if candidate else None
        head = candidate.get("head_commit") if candidate else None
        if candidate_status == "landed":
            return "done"
        if entry.get("status") == "done":
            if not candidate:
                return "done"
            if recorded_commit and head == recorded_commit:
                return "done"
        if candidate_status == "prepared":
            if recorded_commit and head == recorded_commit:
                return "recorded"
            return "pending"
        if entry.get("status") in {"running", "paused", "recorded"}:
            return "paused" if worktree_present else "pending"
        return "pending"

    def sessions(self) -> dict[str, Any]:
        """List every registered campaign from its descriptor files."""
        branch = (
            self.plane_config.get("target_branch")
            or self.plane_config.get("main_branch")
            or "main"
        )
        current = None
        try:
            current = self.campaign
        except SlicemeError:
            current = None
        if current is not None:
            branch = current["target_branch"]
        entries: list[dict[str, Any]] = []
        for key, descriptor in campaign.list_sessions(self.root):
            feature_branch = str(descriptor.get("feature_branch") or key)
            row = self.store.get_campaign(feature_branch)
            campaign_key = row["key"] if row else None
            recorded = (
                {
                    str(candidate["node"])
                    for candidate in self.store.list_candidates(campaign=campaign_key)
                    if candidate.get("node")
                }
                if campaign_key
                else set()
            )
            state = campaign.load_state(self.root, feature_branch)
            dag = campaign.load_dag(self.root, feature_branch) or {}
            total = len([n for n in dag.get("nodes", []) if n.get("id")])
            done = len(recorded)
            entries.append(
                {
                    "feature_branch": feature_branch,
                    "is_current": feature_branch == branch,
                    "campaign": descriptor.get("campaign") or dag.get("campaign"),
                    "label": descriptor.get("label"),
                    "status": descriptor.get("status") or "suspended",
                    "reason": descriptor.get("reason"),
                    "wave": state.get("current_wave")
                    if state.get("current_wave") is not None
                    else descriptor.get("current_wave"),
                    "done": done,
                    "total": total,
                    "suspended_at": descriptor.get("suspended_at"),
                    "session_file": (descriptor.get("pi") or {}).get("session_file"),
                    "descriptor": str(campaign.session_path(self.root, feature_branch)),
                }
            )
        return {"root": str(self.root), "sessions": entries}
