"""Bootstrap and unit verbs for the sliceme engine.

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
    config_path,
    now,
    read_json,
    slugify,
    state_dir,
    worktrees_dir,
    write_json,
)
from .support import (
    _ensure_gitignore,
    _unique_branch,
    _unique_worktree,
    campaign_lock,
)


def _campaign_branch_of(design: str | None, feature_branch: str | None) -> str:
    """The campaign branch: the pull request head.

    An explicit ``feature_branch`` wins.  Otherwise the engine derives
    ``feat/<slug(design-stem)>`` from the design document name.  A caller with
    neither cannot pick a campaign branch, so the engine refuses.
    """
    explicit = str(feature_branch or "").strip()
    if explicit:
        return explicit
    if not design:
        raise SlicemeError(
            "start needs --design (the design document path) or --feature-branch"
        )
    slug = slugify(Path(str(design)).stem)
    return f"feat/{slug}"


class BootstrapVerbs:

    @classmethod
    def init_plane(
        cls,
        root: Path,
        *,
        design: str | None = None,
        feature_branch: str | None = None,
        base: str | None = None,
        checks: list[dict[str, Any]] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Create the on-disk plane (config + state db) for a git repo.

        ``feature_branch`` names the campaign branch (the pull request head).
        When it is absent the engine derives ``feat/<slug(design-stem)>`` from
        ``design``.  ``base`` overrides the delivery base (the pull request
        base); the default is the repository default branch.  The engine refuses
        to use the default branch as the campaign branch.
        """
        from ..util import ensure_parent, state_dir

        if not gitutil.is_git_repo(root):
            raise SlicemeError(f"{root} is not a git repository")
        state = state_dir(root)
        cfg_file = config_path(root)
        if cfg_file.exists() and not force:
            raise SlicemeError(
                "already initialised (.sliceme/config.json exists); use --force to reset config"
            )
        campaign_branch = _campaign_branch_of(design, feature_branch)
        default_branch = integrate.found_default_branch(root)
        delivery_base = base or default_branch
        if base:
            try:
                gitutil.rev_parse(root, base)
            except SlicemeError:
                raise SlicemeError(f"delivery base '{base}' does not exist") from None
        if integrate.is_default_branch(
            root, campaign_branch, {"default_branch": default_branch}
        ):
            raise SlicemeError("the campaign branch cannot be the default branch")
        # The campaign worktree accumulates on the campaign branch, so the
        # worktree branch and the pull request head are one branch.
        worktree_branch = campaign_branch
        config = {
            "version": 1,
            "target_branch": campaign_branch,
            # Deprecated mirror kept for one release; read target_branch first.
            "main_branch": campaign_branch,
            "worktree_branch": worktree_branch,
            "delivery_base": delivery_base,
            "base": delivery_base,
            "default_branch": default_branch,
            "checks": checks or [],
            "policy": {"require_verification": True, "allow_auto_approve": [], "remote": "origin"},
            "created_at": now(),
        }
        ensure_parent(cfg_file)
        write_json(cfg_file, config)
        state.mkdir(parents=True, exist_ok=True)
        store = Store(root)
        try:
            cls._create_plane_campaign(
                store,
                branch=campaign_branch,
                worktree_branch=worktree_branch,
                delivery_base=delivery_base,
                design=design,
            )
            store.conn.commit()
        finally:
            store.close()
        _ensure_gitignore(root)
        return config

    @staticmethod
    def _create_plane_campaign(
        store: Store,
        *,
        branch: str,
        worktree_branch: str | None = None,
        delivery_base: str,
        design: str | None,
    ) -> dict[str, Any]:
        """Create the campaign row for *branch* (idempotent on the branch)."""
        existing = store.get_campaign(branch)
        if existing is not None:
            # A different design must not silently reuse this branch.  Two
            # design names can slugify to one branch, so refuse the collision
            # and let the caller pick a branch with --feature-branch.
            recorded = str(existing.get("design") or "").strip()
            requested = str(design or "").strip()
            if recorded and requested and recorded != requested:
                raise SlicemeError(
                    f"campaign branch '{branch}' already belongs to design "
                    f"'{recorded}'; pass --feature-branch to choose another branch"
                )
            return existing
        key = branch_key(branch)
        by_key = store.get_campaign(key)
        if by_key is not None and by_key["target_branch"] != branch:
            raise SlicemeError(
                f"branch key collision: '{branch}' and "
                f"'{by_key['target_branch']}' share the key '{key}'"
            )
        return store.create_campaign(
            key=key,
            target_branch=branch,
            worktree_branch=worktree_branch or branch,
            delivery_base=delivery_base,
            base=delivery_base,
            design=design,
            unit_name="campaign" if not store.list_campaigns() else f"campaign:{key}",
        )

    @classmethod
    def _register_plane_campaign(
        cls,
        root: Path,
        *,
        design: str | None,
        feature_branch: str | None,
        base: str | None,
    ) -> None:
        """Register a campaign in an existing plane (idempotent on the branch).

        Repeated ``start`` with a different ``feature_branch`` adds a campaign
        to the same plane; the earlier campaigns stay intact.  The config
        mirrors follow the newest campaign branch.
        """
        campaign_branch = _campaign_branch_of(design, feature_branch)
        cfg = read_json(config_path(root)) or {}
        default_branch = cfg.get("default_branch") or integrate.found_default_branch(root)
        delivery_base = base or cfg.get("delivery_base") or default_branch
        if integrate.is_default_branch(
            root, campaign_branch, {"default_branch": default_branch}
        ):
            raise SlicemeError("the campaign branch cannot be the default branch")
        worktree_branch = campaign_branch
        cfg["target_branch"] = campaign_branch
        cfg["main_branch"] = campaign_branch
        cfg["worktree_branch"] = worktree_branch
        cfg["delivery_base"] = delivery_base
        cfg["base"] = delivery_base
        cfg["default_branch"] = default_branch
        write_json(config_path(root), cfg)
        with campaign_lock(root):
            store = Store(root)
            try:
                cls._create_plane_campaign(
                    store,
                    branch=campaign_branch,
                    worktree_branch=worktree_branch,
                    delivery_base=delivery_base,
                    design=design,
                )
                store.conn.commit()
            finally:
                store.close()

    @classmethod
    def init(
        cls,
        path: str | os.PathLike[str] | None = None,
        *,
        name: str | None = None,
        design: str | None = None,
        feature_branch: str | None = None,
        base: str | None = None,
        kind: str = "worker",
        checks: list[dict[str, Any]] | None = None,
        force: bool = False,
        no_unit: bool = False,
    ) -> dict[str, Any]:
        """Bootstrap the plane and a unit for *path* (default cwd), idempotently.

        Safe to call on every session start:

        1. If no plane is found walking up from *path*, create it at the git root.
        2. If *path* is not already inside a unit worktree, create one.

        Returns a summary including ``worktree`` so the caller can bind its
        tools to the unit (the running process cwd is not changed).
        """
        start = Path(path or os.getcwd()).resolve()

        root: Path | None = None
        for candidate in [start, *start.parents]:
            if config_path(candidate).is_file():
                root = candidate
                break

        initialized = False
        if root is None or force:
            if not gitutil.is_git_repo(start):
                raise SlicemeError(f"{start} is not a git repository")
            root = gitutil.toplevel(start)
            cls.init_plane(
                root,
                design=design,
                feature_branch=feature_branch,
                base=base,
                checks=checks,
                force=force,
            )
            initialized = True
        elif design or feature_branch:
            # An existing plane registers one campaign for this design.  A
            # repeated start with a different feature branch adds a campaign;
            # the earlier campaigns stay intact.
            cls._register_plane_campaign(
                root,
                design=design,
                feature_branch=feature_branch,
                base=base,
            )

        # Keep the exclude entry fresh even when the plane already existed and
        # the repo's .git/info/exclude was reset (e.g. re-cloned metadata).
        _ensure_gitignore(root)

        if no_unit:
            return {
                "root": str(root),
                "initialized": initialized,
                "created": False,
                "unit": None,
                "branch": None,
                "worktree": None,
            }

        service = cls(root)
        try:
            created = False
            try:
                unit = service.current_unit(start)
            except SlicemeError:
                existing = {u["name"] for u in service.store.list_units()}
                base_name = name or slugify(start.name) or "session"
                unit_name = base_name
                counter = 2
                while unit_name in existing:
                    unit_name = f"{base_name}-{counter}"
                    counter += 1
                unit = service.create_workspace(unit_name, kind=kind, base=base)
                created = True
        finally:
            service.close()

        return {
            "root": str(root),
            "initialized": initialized,
            "created": created,
            "unit": unit["name"],
            "branch": unit.get("branch"),
            "worktree": unit.get("worktree"),
        }

    def create_workspace(
        self,
        name: str,
        *,
        kind: str = "worker",
        base: str | None = None,
    ) -> dict[str, Any]:
        config = self.config
        base_ref = base or config.get("base") or config.get("main_branch") or "main"
        base_commit = gitutil.rev_parse(self.root, base_ref)

        branch = _unique_branch(self.root, name)
        worktree = _unique_worktree(worktrees_dir(self.root), name)
        gitutil.add_worktree(self.root, worktree, branch=branch, base=base_commit)
        try:
            unit_id = self.store.create_unit(
                name=name,
                kind=kind,
                worktree=str(worktree),
                branch=branch,
                base_commit=base_commit,
            )
        except Exception:
            gitutil.cleanup_worktree(self.root, worktree)
            raise
        self.store.conn.commit()
        return self.store.get_unit(unit_id)  # type: ignore[return-value]

    def list_units(self) -> list[dict[str, Any]]:
        return self.store.list_units()

    def _scoped_units(self) -> list[dict[str, Any]]:
        """Units that belong to the bound campaign (or every unit)."""
        key = self.campaign_key()
        if key is None:
            return self.store.list_units()
        return self.store.list_units(campaign=key)

    def unit_detail(self, unit_ref: str | int) -> dict[str, Any]:
        unit = self.store.require_unit(unit_ref)
        unit["candidates"] = [
            c for c in self.store.list_candidates() if int(c["unit_id"]) == int(unit["id"])
        ]
        return self._project_unit(unit, self.config.get("main_branch"))

    def current_unit(self, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        """Resolve the unit whose worktree contains *path* (default cwd).

        Lets an agent launched inside its own worktree call the CLI without
        passing ``--unit``. Falls back to matching the checked-out branch.
        """
        resolved = Path(path or os.getcwd()).resolve()
        best: dict[str, Any] | None = None
        best_len = -1
        for unit in self.store.list_units():
            try:
                worktree = Path(unit["worktree"]).resolve()
            except (OSError, TypeError):
                continue
            if worktree == resolved or worktree in resolved.parents:
                if len(str(worktree)) > best_len:
                    best, best_len = unit, len(str(worktree))
        if best is not None:
            return best
        branch = gitutil.current_branch(resolved)
        if branch:
            for unit in self.store.list_units():
                if unit["branch"] == branch:
                    return unit
        raise SlicemeError(
            "current directory is not an Sliceme unit worktree; "
            "run `sliceme workspace create` and cd into it, or pass --unit"
        )
