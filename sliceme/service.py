"""Service layer: the single owner of local-plane state.

Every adapter (CLI, pi extension) calls these functions.  This mirrors the
"one engine, many adapters / no adapter owns state" rule in
``docs/guide.md``.  Business rules live in the verb-group mixins
(:mod:`sliceme.verbs`); persistence lives in ``store``; git mutation lives in
``gitutil``.  :func:`sliceme.surface.dispatch` stays the one verb facade.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .store import Store
from .util import (
    SlicemeError,
    branch_key,
    config_path,
    read_json,
)
from .verbs.bootstrap import BootstrapVerbs
from .verbs.campaign import CampaignVerbs
from .verbs.delivery import DeliveryVerbs
from .verbs.review import ReviewVerbs
from .verbs.sessions import SessionVerbs
from .verbs.status import StatusVerbs
from .verbs.support import _default_worktree_branch, campaign_lock  # noqa: F401

__all__ = ["Service", "campaign_lock"]


class Service(
    BootstrapVerbs,
    CampaignVerbs,
    StatusVerbs,
    SessionVerbs,
    ReviewVerbs,
    DeliveryVerbs,
):

    def __init__(
        self,
        root: Path,
        store: Store | None = None,
        *,
        campaign: str | None = None,
        migrate: bool = True,
    ):
        self.root = root
        self.store = store or Store(root, migrate=migrate)
        self._campaign_ref = campaign
        self._campaign: dict[str, Any] | None = None
        if migrate:
            self._ensure_legacy_campaign()

    def close(self) -> None:
        self.store.close()

    @property
    def plane_config(self) -> dict[str, Any]:
        """The plane's ``config.json`` fields only (no campaign)."""
        cfg = read_json(config_path(self.root))
        if cfg is None:
            raise SlicemeError("missing .sliceme/config.json")
        return cfg

    @property
    def campaign(self) -> dict[str, Any] | None:
        """The bound campaign, or the sole campaign, or ``None``.

        A plane with several campaigns and no explicit reference is an error:
        the caller must name one so state cannot silently mix.
        """
        if self._campaign is not None:
            return self._campaign
        if self._campaign_ref is not None:
            self._campaign = self.store.require_campaign(self._campaign_ref)
            return self._campaign
        working = self.store.list_campaigns(state="working")
        if len(working) == 1:
            self._campaign = working[0]
        elif working:
            raise SlicemeError(
                "several campaigns in this plane; pass --campaign <branch>"
            )
        else:
            rows = self.store.list_campaigns()
            if len(rows) == 1:
                self._campaign = rows[0]
            elif rows:
                raise SlicemeError(
                    "several campaigns in this plane; pass --campaign <branch>"
                )
        return self._campaign

    def require_campaign(self) -> dict[str, Any]:
        campaign = self.campaign
        if campaign is None:
            raise SlicemeError("no campaign in this plane; run `sliceme start` first")
        return campaign

    def campaign_key(self) -> str | None:
        campaign = self.campaign
        return campaign["key"] if campaign else None

    @property
    def config(self) -> dict[str, Any]:
        """The effective config: plane fields plus the bound campaign fields.

        Most call sites read ``target_branch`` / ``worktree_branch`` and keep
        working.  Plane-only readers use :attr:`plane_config`.
        """
        cfg = dict(self.plane_config)
        campaign = self.campaign
        if campaign is not None:
            cfg["target_branch"] = campaign["target_branch"]
            cfg["main_branch"] = campaign["target_branch"]
            cfg["worktree_branch"] = campaign["worktree_branch"]
            cfg["base"] = campaign.get("base") or cfg.get("base")
            cfg["campaign_name"] = campaign.get("name")
            cfg["campaign_design"] = campaign.get("design")
            cfg["campaign_key"] = campaign["key"]
        return cfg

    campaign_config = config

    def _ensure_legacy_campaign(self) -> dict[str, Any] | None:
        """Register the one campaign of an old plane (idempotent)."""
        if self.store.list_campaigns():
            return None
        cfg = read_json(config_path(self.root)) or {}
        target = cfg.get("target_branch") or cfg.get("main_branch")
        if not target:
            return None
        worktree_branch = cfg.get("worktree_branch") or _default_worktree_branch(
            self.root, str(target)
        )
        unit = self.store.get_unit("campaign")
        row = self.store.create_campaign(
            key=branch_key(str(target)),
            target_branch=str(target),
            worktree_branch=str(worktree_branch),
            base=cfg.get("base"),
            unit_name="campaign",
        )
        if unit is not None:
            self.store.conn.execute(
                "UPDATE units SET campaign=? WHERE id=? AND campaign IS NULL",
                (row["key"], int(unit["id"])),
            )
            self.store.conn.commit()
        return row
