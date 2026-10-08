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
    _campaign_has_work,
    _default_worktree_branch,
    _ensure_gitignore,
    _resolve_target_branch,
    _unique_branch,
    _unique_worktree,
    campaign_lock,
)


class BootstrapVerbs:

    @classmethod
    def init_plane(
        cls,
        root: Path,
        *,
        main_branch: str | None = None,
        target_branch: str | None = None,
        target_mode: str | None = None,
        worktree_branch: str | None = None,
        base: str | None = None,
        checks: list[dict[str, Any]] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Create the on-disk plane (config + state db) for a git repo.

        ``target_branch`` is the feature branch delivery finally lands on.
        ``target_mode`` is one of ``current``, ``existing``, or ``new``; ``new``
        creates the branch from ``base``.  Delivery refuses to commit to
        ``main``, ``master``, or the recorded default branch, so a plane that
        targets one can be created but never delivered.
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
        target = _resolve_target_branch(
            root,
            target_branch=target_branch or main_branch,
            target_mode=target_mode,
            base=base,
        )
        default_branch = integrate.found_default_branch(root)
        base = base or target
        try:
            gitutil.rev_parse(root, base)
        except SlicemeError:
            raise SlicemeError(f"base branch/ref '{base}' does not exist") from None
        worktree_branch = worktree_branch or _default_worktree_branch(root, target)
        if integrate.is_default_branch(
            root, worktree_branch, {"default_branch": default_branch}
        ):
            raise SlicemeError("the campaign worktree branch cannot be the default branch")
        config = {
            "version": 1,
            "target_branch": target,
            # Deprecated mirror kept for one release; read target_branch first.
            "main_branch": target,
            "worktree_branch": worktree_branch,
            "base": base,
            "default_branch": default_branch,
            "checks": checks or [],
            "policy": {"require_verification": True, "allow_auto_approve": [], "remote": "origin"},
            "created_at": now(),
        }
        ensure_parent(cfg_file)
        write_json(cfg_file, config)
        state.mkdir(parents=True, exist_ok=True)
        store = Store(root)
        store.close()
        _ensure_gitignore(root)
        return config

    @classmethod
    def _sync_campaign_retarget(
        cls,
        root: Path,
        *,
        old_target: str | None = None,
        worktree_branch: str | None = None,
        target_mode: str | None = None,
    ) -> None:
        """Register a campaign for a named target branch.

        A resumed campaign already has a row, so this is a no-op.  A target
        chosen with ``new`` is always a new campaign.  When the caller adopts
        an existing branch and the plane has one campaign with no recorded
        work, that campaign is retargeted in place for backward compatibility.
        """
        cfg = read_json(config_path(root)) or {}
        new_target = cfg.get("target_branch") or cfg.get("main_branch")
        if not new_target:
            return
        with campaign_lock(root):
            store = Store(root)
            try:
                # A plane created by `start --no-unit` has no row yet.
                if not store.list_campaigns() and old_target:
                    store.create_campaign(
                        key=branch_key(str(old_target)),
                        target_branch=str(old_target),
                        worktree_branch=_default_worktree_branch(root, str(old_target)),
                        base=cfg.get("base"),
                        unit_name="campaign",
                    )
                if store.get_campaign(new_target) is not None:
                    store.conn.commit()
                    return
                old = store.get_campaign(old_target) if old_target else None
                if (
                    old is not None
                    and old["target_branch"] != str(new_target)
                    and target_mode != "new"
                    and not _campaign_has_work(store, root, old)
                ):
                    store.update_campaign_target(
                        old["key"],
                        target_branch=str(new_target),
                        worktree_branch=worktree_branch,
                    )
                    store.conn.commit()
                    return
                key = branch_key(str(new_target))
                by_key = store.get_campaign(key)
                if by_key is not None and by_key["target_branch"] != str(new_target):
                    raise SlicemeError(
                        f"branch key collision: '{new_target}' and "
                        f"'{by_key['target_branch']}' share the key '{key}'"
                    )
                store.create_campaign(
                    key=key,
                    target_branch=str(new_target),
                    worktree_branch=str(
                        worktree_branch
                        or _default_worktree_branch(root, str(new_target))
                    ),
                    base=cfg.get("base"),
                    unit_name=f"campaign:{key}",
                )
                store.conn.commit()
            finally:
                store.close()

    @classmethod
    def _retarget_plane(
        cls,
        root: Path,
        *,
        main_branch: str | None = None,
        target_branch: str | None = None,
        target_mode: str | None = None,
        worktree_branch: str | None = None,
        base: str | None = None,
    ) -> None:
        """Point an existing plane's target branch at an existing branch.

        Used by ``start --target`` so a campaign adopts the chosen branch even
        when the plane already exists.  It never creates a branch unless
        ``target_mode`` is ``new``: a missing one is an error.
        """
        target = _resolve_target_branch(
            root,
            target_branch=target_branch or main_branch,
            target_mode=target_mode,
            base=base,
        )
        cfg = read_json(config_path(root))
        if cfg is None:
            raise SlicemeError("missing .sliceme/config.json")
        default_branch = cfg.get("default_branch") or integrate.found_default_branch(root)
        cfg["target_branch"] = target
        cfg["main_branch"] = target
        cfg["default_branch"] = default_branch
        cfg["base"] = base or target
        if worktree_branch:
            cfg["worktree_branch"] = worktree_branch
        write_json(config_path(root), cfg)

    @classmethod
    def init(
        cls,
        path: str | os.PathLike[str] | None = None,
        *,
        name: str | None = None,
        base: str | None = None,
        kind: str = "worker",
        main_branch: str | None = None,
        target_branch: str | None = None,
        target_mode: str | None = None,
        worktree_branch: str | None = None,
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
                main_branch=main_branch,
                target_branch=target_branch,
                target_mode=target_mode,
                worktree_branch=worktree_branch,
                base=base,
                checks=checks,
                force=force,
            )
            initialized = True
        elif main_branch or target_branch or target_mode:
            # An existing plane keeps its identity; adopting a target branch
            # registers a campaign.  A resumed campaign already has a row, so
            # the registration is a no-op; a different target is a new
            # campaign and the earlier one stays intact.
            old_cfg = read_json(config_path(root)) or {}
            old_target = old_cfg.get("target_branch") or old_cfg.get("main_branch")
            cls._retarget_plane(
                root,
                main_branch=main_branch,
                target_branch=target_branch,
                target_mode=target_mode,
                worktree_branch=worktree_branch,
                base=base,
            )
            cls._sync_campaign_retarget(
                root,
                old_target=old_target,
                worktree_branch=worktree_branch,
                target_mode=target_mode,
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
