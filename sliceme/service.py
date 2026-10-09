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
from . import integrate
from .gitutil import workspace_lock  # noqa: F401
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

__all__ = ["Service", "workspace_lock"]


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

        The canonical keys are ``work_branch`` (the pull request head) and
        ``base_branch`` (the pull request base).  ``default_branch`` stays the
        separate protected branch.  The retired names (``target_branch``,
        ``main_branch``, ``worktree_branch``, ``feature_branch``,
        ``delivery_base``, ``base``) mirror the new ones for one release.
        Plane-only readers use :attr:`plane_config`.
        """
        cfg = dict(self.plane_config)
        campaign = self.campaign
        work_branch = (
            campaign.get("work_branch") if campaign else None
        ) or integrate.work_branch_of(cfg)
        base_branch = (
            campaign.get("base_branch") if campaign else None
        ) or integrate.base_branch_of(cfg)
        if campaign is not None:
            cfg["campaign_name"] = campaign.get("name")
            cfg["campaign_design"] = campaign.get("design")
            cfg["campaign_key"] = campaign["key"]
        cfg["work_branch"] = work_branch
        cfg["base_branch"] = base_branch
        # Read-time shim for one release: the retired names mirror the new ones.
        cfg["target_branch"] = work_branch
        cfg["main_branch"] = work_branch
        cfg["worktree_branch"] = work_branch
        cfg["feature_branch"] = work_branch
        cfg["delivery_base"] = base_branch
        cfg["base"] = base_branch
        return cfg

    campaign_config = config

    def _ensure_legacy_campaign(self) -> dict[str, Any] | None:
        """Register the one campaign of an old plane (idempotent)."""
        if self.store.list_campaigns():
            return None
        cfg = read_json(config_path(self.root)) or {}
        target = (
            cfg.get("work_branch")
            or cfg.get("worktree_branch")
            or cfg.get("target_branch")
            or cfg.get("main_branch")
            or cfg.get("feature_branch")
        )
        if not target:
            return None
        work_branch = str(target)
        row = self.store.create_campaign(
            key=branch_key(work_branch),
            work_branch=work_branch,
            base_branch=integrate.base_branch_of(cfg),
            unit_name="campaign",
        )
        return row
