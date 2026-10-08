"""Review and approval verbs for the sliceme engine.

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
    branch_key,
)


class ReviewVerbs:

    def review_snapshot(self, *, commit: str | None = None) -> dict[str, Any]:
        from ..review import packet

        return packet.build_packet(self, commit=commit)

    def review_decision(
        self,
        *,
        action: str,
        commit: str | None = None,
        all_commits: bool = False,
        actor: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Append one campaign-level decision (``commit_hash`` is NULL).

        One approval controls the whole campaign commit set, so ``approve``
        and ``request_changes`` never bind to one commit.  ``override`` stays
        a separate campaign-level decision that admits delivery.
        """
        from ..review import packet

        if action not in {"approve", "request_changes", "override"}:
            raise SlicemeError(
                "decision must be one of: approve, request_changes, override"
            )
        branch_key = packet.campaign_branch_key(self)
        if action == "approve":
            return self._approve_all(branch_key, actor=actor, note=note)
        if action == "request_changes" and not (note or "").strip():
            raise SlicemeError("request_changes needs a note")
        if action == "override" and not (note or "").strip():
            raise SlicemeError("override needs a note")
        decision = self.store.add_review_decision(
            branch_key=branch_key,
            commit_hash=None,
            action=action,
            actor=actor,
            note=note,
        )
        self.store.conn.commit()
        return decision

    def _approve_all(
        self, branch_key: str, *, actor: str | None, note: str | None
    ) -> dict[str, Any]:
        """Record ONE campaign-level approve decision."""
        decision = self.store.add_review_decision(
            branch_key=branch_key,
            commit_hash=None,
            action="approve",
            actor=actor,
            note=note,
        )
        self.store.conn.commit()
        return decision

    def campaign_decision(self) -> dict[str, Any] | None:
        """The latest campaign-level decision (``commit_hash`` is NULL)."""
        from ..review import packet

        return self.store.latest_review_decision(
            packet.campaign_branch_key(self), None
        )

    def campaign_approved(self) -> bool:
        """Whether the newest campaign-level decision is an unconsumed approve."""
        return self._is_approved(self.campaign_decision())

    def unapproved_commits(self) -> list[str]:
        """Campaign commits that still need approval.

        Approval is campaign-level, so either every commit is unapproved or
        none is.
        """
        from ..review import packet

        if self.campaign_approved():
            return []
        return packet.review_commits(self)

    @staticmethod
    def _is_approved(decision: dict[str, Any] | None) -> bool:
        return bool(
            decision
            and decision.get("action") == "approve"
            and decision.get("consumed_at") is None
        )

    def require_all_approved(
        self, commits: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Refuse delivery unless the campaign is approved (or overridden).

        Returns the override decision when one admitted delivery, else ``None``.
        """
        from ..review import packet

        commits = commits if commits is not None else packet.campaign_commits(self)
        if not commits:
            return None
        decision = self.campaign_decision()
        if self._is_approved(decision):
            return None
        if (
            decision
            and decision.get("action") == "override"
            and (decision.get("note") or "").strip()
        ):
            return decision
        listing = ", ".join(commit[:7] for commit in commits)
        error = SlicemeError(
            f"not-approved: {listing} not approved; open `sliceme review`"
            " and approve the campaign"
        )
        error.reason = "not_approved"
        raise error

    def consume_approvals(self, commits: list[str] | None = None) -> None:
        """Mark the latest campaign decision consumed after a landed delivery."""
        decision = self.campaign_decision()
        if decision is not None:
            self.store.consume_review_decisions([int(decision["id"])])
            self.store.conn.commit()
