"""Review and approval verbs for the sliceme engine.

Internal module: :class:`sliceme.service.Service` composes this mixin and
:func:`sliceme.surface.dispatch` stays the one verb facade.

The approval is folded into delivery.  ``deliver`` records the one
campaign-level approval that it consumes, so there is no separate approve
step.  The read helpers serve the review packet.
"""

from __future__ import annotations

from typing import Any


class ReviewVerbs:

    def review_snapshot(self, *, commit: str | None = None) -> dict[str, Any]:
        from ..review import packet

        return packet.build_packet(self, commit=commit)

    def record_delivery_approval(
        self, *, actor: str | None = None, note: str | None = None
    ) -> dict[str, Any]:
        """Record the one campaign-level approval that a delivery consumes.

        ``commit_hash`` is NULL, so the decision covers the whole campaign
        commit set.  ``deliver`` calls this after the human confirmation, then
        consumes the row when the delivery lands.
        """
        from ..review import packet

        decision = self.store.add_approval(
            branch_key=packet.campaign_branch_key(self),
            commit_hash=None,
            action="approve",
            actor=actor,
            note=note,
        )
        self.store.conn.commit()
        return decision

    def campaign_approval(self) -> dict[str, Any] | None:
        """The latest campaign-level decision (``commit_hash`` is NULL)."""
        from ..review import packet

        return self.store.latest_approval(
            packet.campaign_branch_key(self), None
        )

    def campaign_approved(self) -> bool:
        """Whether the newest campaign-level decision is an unconsumed approve."""
        return self._is_approved(self.campaign_approval())

    @staticmethod
    def _is_approved(decision: dict[str, Any] | None) -> bool:
        return bool(
            decision
            and decision.get("action") == "approve"
            and decision.get("consumed_at") is None
        )
