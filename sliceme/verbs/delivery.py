"""Delivery, report, and cleanup verbs for the sliceme engine.

Internal module: :class:`sliceme.service.Service` composes this mixin and
:func:`sliceme.surface.dispatch` stays the one verb facade.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import campaign, gitutil, integrate, sandbox
from ..store import Store
from ..util import (
    SlicemeError,
    now,
    state_dir,
)
from .support import (
    _delivery_lock,
)


class DeliveryVerbs:

    def deliver(
        self,
        *,
        target: str | None = None,
        source: str | None = None,
        cleanup: str = "none",
        run_checks_flag: bool = True,
    ) -> dict[str, Any]:
        """Push the campaign worktree branch and open the delivery pull request.

        This is the single, end-of-campaign delivery.  The target branch is
        never the default branch and there is no override.  Once the pull
        request is open, the campaign is done.
        """
        if cleanup not in {"none", "worktrees", "all"}:
            raise SlicemeError("cleanup must be one of: none, worktrees, all")
        from ..review import packet

        target_branch = target or self.config.get("target_branch") or self.config.get("main_branch")
        source_branch = source or self.config.get("worktree_branch") or ""
        key = self.campaign_key()
        stored = self.store.get_campaign(key) if key else None
        if stored and stored.get("pr_url"):
            # Idempotent: the pull request is open, so the campaign is done.
            results = [
                integrate.landed(
                    f"pull request already open: {stored['pr_url']}",
                    {"url": stored["pr_url"], "number": stored.get("pr_number")},
                )
            ]
        else:
            commits = packet.campaign_commits(self)
            with _delivery_lock(self.root):
                override = self.require_all_approved(commits)
                results = integrate.deliver_pull_request(
                    self.store,
                    self.root,
                    self.config,
                    campaign=key,
                    target=target,
                    source=source,
                    run_checks_flag=run_checks_flag,
                )
                if results and all(result.status == "landed" for result in results):
                    self.consume_approvals(commits)
                    if override:
                        self.store.consume_review_decisions([int(override["id"])])
                        self.store.conn.commit()
                    if key:
                        pull_request = results[0].pull_request or {}
                        if pull_request.get("url"):
                            self.store.set_campaign_pull_request(
                                key,
                                url=str(pull_request["url"]),
                                number=pull_request.get("number"),
                            )
                        self.store.set_campaign_state(key, "delivered")
                        self.store.conn.commit()
                    self._campaign = None
        cleanup_result: dict[str, Any] | None = None
        artifacts_removed: list[str] = []
        if cleanup in {"worktrees", "all"}:
            cleanup_result = self.gc()
        if cleanup == "all":
            artifacts_removed = self.remove_campaign_artifacts(keep_report=True)
        return {
            "target_branch": target_branch,
            "source": source_branch,
            "results": [r.to_dict() for r in results],
            "pull_request": results[0].pull_request if results else None,
            "cleanup": cleanup_result,
            "artifacts_removed": artifacts_removed,
        }

    def report(
        self, *, narrative: str | None = None, design: str | None = None
    ) -> dict[str, Any]:
        """Write the deterministic campaign report plus an optional narrative."""
        return campaign.write_report(
            self.root,
            self.config,
            self.store,
            campaign=self.campaign_key(),
            narrative=narrative,
            design=design,
        )

    def remove_campaign_artifacts(
        self, *, branch: str | None = None, keep_report: bool = True
    ) -> list[str]:
        """Delete every per-campaign file.  Keep the report by default."""
        branch = branch or self.config.get("main_branch") or "main"
        state = state_dir(self.root)
        key = campaign.branch_key(branch)
        paths = [
            campaign.dag_path(self.root, branch),
            campaign.state_path(self.root, branch),
            campaign.session_path(self.root, branch),
            campaign.control_path(self.root, branch),
            *sorted(state.glob(f"{key}.worker_*.log")),
        ]
        if not keep_report:
            paths.append(campaign.report_path(self.root, branch))
        return self._remove_files(paths)

    @staticmethod
    def _remove_files(paths: list[Path]) -> list[str]:
        removed: list[str] = []
        for path in paths:
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink()
                    removed.append(str(path))
            except OSError:
                continue
        return removed

    def gc(self, *, artifacts: bool = False) -> dict[str, Any]:
        """Prune worktrees, branches, scratch, and reviews.

        ``artifacts`` also removes the files of finished campaigns (the report
        stays).  ``status --gc`` sets it; ``deliver --cleanup worktrees`` does
        not, so it keeps the dag and state records.
        """
        removed = []
        pruned_branches = []
        for unit in self.store.list_units():
            if unit["state"] not in {"landed", "closed"}:
                continue
            path = Path(unit["worktree"])
            branch = unit.get("branch")
            registered = bool(branch) and gitutil.worktree_for_branch(self.root, branch) is not None
            if path.exists() or registered:
                removed.append(unit["name"])
            # Landed content is already on main as a squashed commit, so its
            # `sliceme/<unit>` branch is disposable; otherwise one branch leaks per
            # landed unit.  Closed units may hold unmerged work, so keep their
            # branches.  ``cleanup_worktree`` prunes stale metadata before
            # deleting the branch, so a hand-deleted worktree no longer blocks it.
            drop_branch = unit["state"] == "landed" and branch
            branch_existed = bool(drop_branch) and gitutil.branch_exists(self.root, branch)
            gitutil.cleanup_worktree(
                self.root, path, branch=branch if drop_branch else None
            )
            if branch_existed:
                pruned_branches.append(branch)
        gitutil.prune_worktrees(self.root)
        from ..util import rmtree

        scratch = self.root / ".sliceme" / "scratch"
        rmtree(scratch)
        pruned_artifacts = self._prune_finished_artifacts() if artifacts else []
        pruned_reviews = self._prune_reviews()
        self.store.conn.commit()
        return {
            "removed_worktrees": removed,
            "pruned_branches": pruned_branches,
            "pruned_artifacts": pruned_artifacts,
            "pruned_reviews": pruned_reviews,
        }

    def _prune_finished_artifacts(self) -> list[str]:
        """Remove the files of campaigns that have finished (report kept).

        A campaign has finished when the store records it as landed or
        delivered, or when its descriptor marks it completed.  The descriptor
        pass also reaches old campaigns that predate the campaign registry.
        """
        branches = {
            str(row["target_branch"])
            for row in self.store.list_campaigns()
            if str(row.get("state")) in {"landed", "delivered"}
        }
        branches.update(
            str(descriptor.get("feature_branch") or key)
            for key, descriptor in campaign.list_sessions(self.root)
            if str(descriptor.get("status")) == "completed"
        )
        return [
            name
            for branch in sorted(branches)
            for name in self.remove_campaign_artifacts(branch=branch)
        ]

    def _prune_reviews(self) -> dict[str, int]:
        """Prune old review rows for campaigns that are gone and unregistered.

        Every key in the campaign registry, every descriptor file, and the
        current campaign are kept, so an active review is never lost.
        """
        policy = self.plane_config.get("policy") or {}
        days = float(policy.get("review_retention_days") or 30)
        keep = {
            campaign.branch_key(str(descriptor.get("feature_branch") or key))
            for key, descriptor in campaign.list_sessions(self.root)
        }
        keep.update(row["key"] for row in self.store.list_campaigns())
        return self.store.prune_reviews(keep_branch_keys=keep, keep_after=now() - days * 86400)
