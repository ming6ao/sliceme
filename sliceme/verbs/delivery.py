"""Delivery, report, and cleanup verbs for the sliceme engine.

Internal module: :class:`sliceme.service.Service` composes this mixin and
:func:`sliceme.surface.dispatch` stays the one verb facade.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import campaign, gitutil, integrate
from ..gitutil import workspace_lock
from ..util import (
    SlicemeError,
    now,
    state_dir,
)


class DeliveryVerbs:

    def deliver(
        self,
        *,
        source: str | None = None,
        cleanup: str = "none",
        run_checks_flag: bool = True,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Deliver the campaign with the selected mode.

        ``pull_request`` pushes the campaign branch and opens a pull request.
        ``local`` merges the campaign branch into the local base branch and
        stops, so the user pushes the base branch later.  Both modes run under
        the one approval gate.  The campaign branch is never the default
        branch and there is no override.
        """
        if cleanup not in {"none", "worktrees", "all"}:
            raise SlicemeError("cleanup must be one of: none, worktrees, all")
        from ..review import packet

        key = self.campaign_key()
        stored = self.store.get_campaign(key) if key else None
        resolved_mode = integrate.resolve_delivery_mode(self.config, mode=mode)
        feature_branch = self.config.get("work_branch") or ""
        source_branch = source or self.config.get("work_branch") or ""

        done = self._already_delivered(stored, resolved_mode)
        if done is not None:
            results = [done]
        else:
            commits = packet.campaign_commits(self)
            with workspace_lock(self.root):
                override = self.require_all_approved(commits)
                if key:
                    self.store.set_campaign_delivery_mode(key, resolved_mode)
                results = integrate.deliver(
                    self.store,
                    self.root,
                    self.config,
                    campaign=key,
                    source=source,
                    run_checks_flag=run_checks_flag,
                    mode=resolved_mode,
                )
                if results and all(result.status == "delivered" for result in results):
                    self.consume_approvals(commits)
                    if override:
                        self.store.consume_approvals([int(override["id"])])
                        self.store.conn.commit()
                    if key:
                        first = results[0]
                        pull_request = first.pull_request or {}
                        if pull_request.get("url"):
                            self.store.set_campaign_pull_request(
                                key,
                                url=str(pull_request["url"]),
                                number=pull_request.get("number"),
                            )
                        if first.merge_commit:
                            self.store.set_campaign_merge_commit(
                                key, first.merge_commit
                            )
                        state = "landed" if resolved_mode == "local" else "delivered"
                        self.store.set_campaign_state(key, state)
                        self.store.conn.commit()
                    self._campaign = None
        cleanup_result: dict[str, Any] | None = None
        artifacts_removed: list[str] = []
        if cleanup in {"worktrees", "all"}:
            cleanup_result = self.gc()
        if cleanup == "all":
            artifacts_removed = self.remove_campaign_artifacts(keep_report=True)
        return {
            "work_branch": feature_branch,
            "feature_branch": feature_branch,
            # Deprecated mirror kept for one release; read work_branch first.
            "target_branch": feature_branch,
            "base_branch": self.config.get("base_branch"),
            "mode": resolved_mode,
            "source": source_branch,
            "results": [r.to_dict() for r in results],
            "pull_request": results[0].pull_request if results else None,
            "merge_commit": results[0].merge_commit if results else None,
            "cleanup": cleanup_result,
            "artifacts_removed": artifacts_removed,
        }

    def _already_delivered(
        self, stored: dict[str, Any] | None, mode: str
    ) -> "integrate.DeliveryResult | None":
        """The result for a campaign that already finished delivery, else None."""
        if not stored:
            return None
        if mode == "local":
            if stored.get("state") == "landed":
                return integrate.local_result(
                    f"already merged into local "
                    f"{stored.get('base_branch') or 'base'}",
                    merge_commit=stored.get("merge_commit"),
                )
            return None
        if stored.get("pr_url"):
            # Idempotent: the pull request is open, so the campaign is done.
            return integrate.delivered_result(
                f"pull request already open: {stored['pr_url']}",
                {"url": stored["pr_url"], "number": stored.get("pr_number")},
            )
        return None

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

    def evidence(self, *, design: str | None = None) -> dict[str, Any]:
        """Write the deterministic evidence document for the campaign.

        The engine writes ``.sliceme/<key>.evidence.json`` (the full check
        output) and ``.sliceme/<key>.evidence.md`` (the bounded output).  The
        Markdown document is the pull request body.  This action is not a gate.
        """
        return campaign.write_evidence(
            self.root,
            self.config,
            self.store,
            campaign=self.campaign_key(),
            design=design,
        )

    def remove_campaign_artifacts(
        self, *, branch: str | None = None, keep_report: bool = True
    ) -> list[str]:
        """Delete every per-campaign file.  Keep the report by default."""
        branch = branch or self.config.get("work_branch") or "main"
        state = state_dir(self.root)
        key = campaign.branch_key(branch)
        paths = [
            campaign.dag_path(self.root, branch),
            campaign.session_path(self.root, branch),
            campaign.control_path(self.root, branch),
            campaign.budget_path(self.root, branch),
            *sorted(state.glob(f"{key}.worker_*.log")),
        ]
        if not keep_report:
            paths.append(campaign.report_path(self.root, branch))
            paths.append(campaign.evidence_json_path(self.root, branch))
            paths.append(campaign.evidence_md_path(self.root, branch))
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
        stays).  ``gc`` sets it; ``deliver --cleanup worktrees`` does
        not, so it keeps the dag and state records.
        """
        removed = []
        pruned_branches = []
        # The campaign owns the worktree and the delivery state, so gc reads the
        # campaign registry rather than the legacy per-unit state.
        for row in self.store.list_campaigns():
            state = str(row.get("state") or "")
            if state not in {"landed", "delivered", "closed"}:
                continue
            recorded = row.get("worktree")
            if not recorded:
                continue
            path = Path(recorded)
            branch = row.get("work_branch")
            registered = bool(branch) and gitutil.worktree_for_branch(self.root, branch) is not None
            if path.exists() or registered:
                removed.append(str(row["key"]))
            # A delivered pull request branch is disposable: the work is on the
            # forge.  A landed campaign branch stays until the user pushes the
            # local base branch, so the merge keeps a second reference.  Closed
            # campaigns may hold unmerged work, so keep their branches.
            # ``cleanup_worktree`` prunes stale metadata before deleting the
            # branch, so a hand-deleted worktree no longer blocks it.
            drop_branch = bool(branch) and (
                state == "delivered"
                or (state == "landed" and self._landed_is_pushed(row))
            )
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
        pruned_approvals = self._prune_approvals()
        self.store.conn.commit()
        return {
            "removed_worktrees": removed,
            "pruned_branches": pruned_branches,
            "pruned_artifacts": pruned_artifacts,
            "pruned_approvals": pruned_approvals,
        }

    def _landed_is_pushed(self, row: dict[str, Any]) -> bool:
        """Whether a landed campaign's merge commit is on the remote base.

        The user pushes the base branch by hand after a local merge.  The
        remote ref is the signal that the push happened, so the campaign
        branch is safe to drop.
        """
        merge_commit = row.get("merge_commit")
        base = row.get("base_branch")
        if not merge_commit or not base:
            return False
        remote = (self.plane_config.get("policy") or {}).get("remote") or "origin"
        ref = f"refs/remotes/{remote}/{base}"
        res = gitutil.git(
            self.root, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False
        )
        if not res.ok:
            return False
        return (
            gitutil.merge_base(self.root, res.stdout.strip(), str(merge_commit))
            == str(merge_commit)
        )

    def _prune_finished_artifacts(self) -> list[str]:
        """Remove the files of campaigns that have finished (report kept).

        A campaign has finished when the store records it as landed or
        delivered, or when its descriptor marks it completed.  The descriptor
        pass also reaches old campaigns that predate the campaign registry.
        """
        branches = {
            str(row["work_branch"])
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

    def _prune_approvals(self) -> dict[str, int]:
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
        return self.store.prune_approvals(keep_branch_keys=keep, keep_after=now() - days * 86400)
