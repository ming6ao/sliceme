"""Service layer: the single owner of local-plane state.

Every adapter (CLI, pi extension) calls these functions.  This mirrors the
"one engine, many adapters / no adapter owns state" rule in
``docs/guide.md``.  Business rules live here; persistence lives in
``store``; git mutation lives in ``gitutil``.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import campaign, gitutil, integrate, sandbox
from .ownership import (
    DEFAULT_WAVE_SIZE,
    merge_same_own_nodes,
    node_owns,
    path_within_owns,
    plan_dag_waves,
    validate_dag,
)
from .store import Store
from .util import (
    SlicemeError,
    branch_key,
    config_path,
    now,
    read_json,
    slugify,
    worktrees_dir,
    write_json,
)


def _subject_line(text: str | None) -> str:
    """Return the first non-empty, whitespace-collapsed line of *text*."""
    for line in str(text or "").splitlines():
        collapsed = " ".join(line.split())
        if collapsed:
            return collapsed
    return ""


# A running node is stalled when its heartbeat is older than this many seconds.
STALLED_AFTER_SECONDS = 5.0


def _metric_number(value: Any) -> float:
    """Parse one metric value; a bad value becomes 0."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _metric_object(value: Any) -> dict[str, Any]:
    """Parse one JSON object column, as a dict or as JSON text."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _metric_list(value: Any) -> list[Any]:
    """Parse one JSON list column, as a list or as JSON text."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _metric_duration(row: dict[str, Any], ts: float) -> float:
    """The wall clock of one attempt row, even while it still runs."""
    duration = _metric_number(row.get("duration"))
    if duration > 0:
        return duration
    started = _metric_number(row.get("started_at"))
    if not started:
        return 0.0
    finished = row.get("finished_at")
    end = _metric_number(finished) if finished else ts
    return max(0.0, end - started)


def _read_heartbeat(path: Path) -> dict[str, Any]:
    """Read one per-node heartbeat file; a bad file becomes empty."""
    data = read_json(path)
    return data if isinstance(data, dict) else {}


class Service:
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

    # The design document's name; the property is the same object.
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

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------
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
        from .util import ensure_parent, state_dir

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
            "policy": {"require_verification": True, "allow_auto_approve": []},
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
        with _campaign_lock(root):
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

    # ------------------------------------------------------------------
    # Units
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Campaign scope: one worktree for the whole campaign
    # ------------------------------------------------------------------
    def create_campaign_workspace(self, *, base: str | None = None) -> dict[str, Any]:
        """Create (or reuse) the campaign worktree for the bound campaign.

        Every wave commits onto this one branch.  It is never recreated and
        never rebased, so files written by an earlier wave are still present
        when the next wave runs.
        """
        campaign_row = self.campaign
        if campaign_row is None:
            campaign_row = self._ensure_legacy_campaign()
        if campaign_row is None:
            raise SlicemeError("no campaign in this plane; run `sliceme start` first")
        key = str(campaign_row["key"])
        name = str(campaign_row["unit_name"])
        existing = self.store.get_unit_by_campaign(key)
        if existing is not None:
            worktree = Path(existing["worktree"])
            if worktree.exists():
                return existing
            return self._recreate_campaign_worktree(existing, base=base)
        config = self.config
        base_ref = (
            base
            or campaign_row.get("base")
            or config.get("base")
            or campaign_row["target_branch"]
        )
        base_commit = gitutil.rev_parse(self.root, base_ref)
        branch = str(campaign_row.get("worktree_branch") or "").strip()
        if not branch:
            branch = _default_worktree_branch(self.root, str(campaign_row["target_branch"]))
            self.store.update_campaign_target(
                key,
                target_branch=str(campaign_row["target_branch"]),
                worktree_branch=branch,
            )
            self.store.conn.commit()
        if integrate.is_default_branch(self.root, branch, config):
            raise SlicemeError(
                f"refusing to use the default branch '{branch}' as the campaign worktree"
            )
        worktree = worktrees_dir(self.root) / f"campaign-{key}"
        gitutil.cleanup_worktree(self.root, worktree)
        if gitutil.branch_exists(self.root, branch):
            gitutil.add_worktree(
                self.root, worktree, branch=branch, base=branch, new_branch=False
            )
        else:
            gitutil.add_worktree(self.root, worktree, branch=branch, base=base_commit)
        try:
            unit_id = self.store.create_unit(
                name=name,
                kind="campaign",
                worktree=str(worktree),
                branch=branch,
                base_commit=base_commit,
                campaign=key,
            )
        except Exception:
            gitutil.cleanup_worktree(self.root, worktree)
            raise
        self.store.conn.commit()
        return self.store.get_unit(unit_id)  # type: ignore[return-value]

    def _recreate_campaign_worktree(
        self, existing: dict[str, Any], *, base: str | None = None
    ) -> dict[str, Any]:
        """Recreate a campaign worktree whose directory was deleted by hand."""
        worktree = Path(existing["worktree"])
        branch = str(existing["branch"])
        gitutil.cleanup_worktree(self.root, worktree)
        if gitutil.branch_exists(self.root, branch):
            gitutil.add_worktree(
                self.root, worktree, branch=branch, base=branch, new_branch=False
            )
        else:
            base_ref = (
                base
                or existing.get("base_commit")
                or self.config.get("base")
                or self.config.get("target_branch")
                or "main"
            )
            gitutil.add_worktree(
                self.root, worktree, branch=branch, base=gitutil.rev_parse(self.root, base_ref)
            )
        key = existing.get("campaign")
        if key:
            return self.store.get_unit_by_campaign(str(key))  # type: ignore[return-value]
        return self.store.get_unit(existing["name"])  # type: ignore[return-value]

    def create_wave_workspace(
        self, wave_index: int, *, base: str | None = None
    ) -> dict[str, Any]:
        """Compatibility alias: every wave shares the one campaign worktree."""
        return self.create_campaign_workspace(base=base)

    def wave_unit(self, wave_index: int) -> dict[str, Any] | None:
        campaign_row = self.campaign
        if campaign_row is None:
            return self.store.get_unit("campaign")
        return self.store.get_unit_by_campaign(str(campaign_row["key"]))

    def record_wave(
        self,
        wave_index: int,
        *,
        messages: dict[str, str] | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """Record a shared wave worktree: conformance, then per-node commits.

        Every changed path is attributed to exactly one same-wave node by its
        owned directories; each node gets one commit and a prepared candidate
        on the shared wave branch.  The commit subject is the node's per-node
        ``messages`` entry.  A node with changes and no description is an
        error.
        """
        branch = self.config.get("main_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        if not dag or not dag.get("nodes"):
            raise SlicemeError("record_wave needs a campaign DAG")
        validate_dag(list(dag["nodes"]))
        wave_size = int(dag.get("concurrency") or DEFAULT_WAVE_SIZE)
        waves = plan_dag_waves(list(dag["nodes"]), wave_size=wave_size)
        wave = next((w for w in waves if w.index == int(wave_index)), None)
        if wave is None:
            raise SlicemeError(f"unknown wave: {wave_index}")
        unit = self.wave_unit(wave_index)
        if unit is None:
            raise SlicemeError(
                f"wave {wave_index} has no workspace; run `sliceme wave --open` first"
            )
        by_id = {str(node["id"]): node for node in dag["nodes"]}
        members = [by_id[node_id] for node_id in wave.members if node_id in by_id]
        return self._record_wave_commits(
            unit,
            int(wave_index),
            members,
            messages=messages,
            summary=summary,
        )

    def _record_wave_commits(
        self,
        unit: dict[str, Any],
        wave_index: int,
        members: list[dict[str, Any]],
        *,
        messages: dict[str, str] | None,
        summary: str | None,
    ) -> dict[str, Any]:
        worktree = Path(unit["worktree"])
        if not worktree.exists():
            raise SlicemeError(f"wave worktree missing: {worktree}")
        gitutil.git(worktree, "add", "-A", check=False)
        # Diff against the current HEAD, not the fork point, so an earlier
        # wave's committed changes are not re-attributed to this wave.  The
        # campaign worktree accumulates commits across waves.
        entries = _changed_entries(worktree)
        owners = {str(node["id"]): node_owns(node) for node in members}
        assignment: dict[str, list[str]] = {node_id: [] for node_id in owners}
        violations: list[str] = []
        for status, path, old in entries:
            new_owners = _owners_of(path, owners)
            old_owners = _owners_of(old, owners) if old else new_owners
            if (
                len(new_owners) != 1
                or len(old_owners) != 1
                or new_owners[0] != old_owners[0]
            ):
                violations.append(_describe_violation(status, path, old, new_owners, old_owners))
                continue
            node_id = new_owners[0]
            assignment[node_id].append(path)
            if old:
                assignment[node_id].append(old)
        if violations:
            raise SlicemeError(
                "wave conformance failed; every changed path must map to exactly one "
                "wave node's owned directories: " + "; ".join(violations[:10])
            )
        messages = messages or {}
        created: list[dict[str, Any]] = []
        for node in members:
            node_id = str(node["id"])
            paths = sorted(set(assignment.get(node_id) or []))
            if not paths:
                continue
            # The subject is the human description, never a wave prefix.  A
            # node with changes must have its own description; Sliceme never
            # invents one.
            description = _subject_line(messages.get(node_id))
            if not description:
                raise SlicemeError(
                    f"wave --record: node '{node_id}' has no description; "
                    f"pass --messages '{{\"{node_id}\": \"...\"}}'"
                )
            result = gitutil.git(
                worktree, "commit", "-m", description, "--", *paths, check=False
            )
            if not result.ok:
                raise SlicemeError(
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"commit failed for {node_id}"
                )
            head = gitutil.head_commit(worktree)
            cid = self.store.create_candidate(
                unit_id=int(unit["id"]),
                head_commit=head,
                summary=summary,
                node=node_id,
                campaign=self.campaign_key(),
            )
            created.append(self.store.get_candidate(cid))
        self.store.conn.commit()
        return {
            "wave": int(wave_index),
            "unit": unit["name"],
            "branch": unit["branch"],
            "worktree": str(worktree),
            "candidates": created,
            "changed": [path for _, path, _ in entries],
        }

    def simulation(self, *, run_checks_flag: bool = True) -> dict[str, Any]:
        return integrate.simulate(
            self.store,
            self.root,
            self.config,
            campaign=self.campaign_key(),
            run_checks_flag=run_checks_flag,
        )

    def executor(self):
        """Build the single sandboxed verification executor for this plane."""
        from .executor import Executor

        branch = self.config.get("main_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        return Executor(
            self.root, self.store, self.config, dag=dag, campaign=self.campaign_key()
        )

    def sandbox_info(self, *, gpu_required: bool = False) -> dict[str, Any]:
        """Resolve and validate the project sandbox gate (never raises)."""
        branch = self.config.get("main_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        required = sandbox.is_required(dag, self.config)
        try:
            profile = sandbox.require_sandbox(
                dag, self.config, root=self.root, gpu_required=gpu_required
            )
        except SlicemeError as exc:
            return {"ok": False, "required": required, "error": str(exc)}
        return {
            "ok": True,
            "required": required,
            "manifest": profile.manifest,
            "digest": profile.digest(),
            "sandbox": profile.to_dict(),
        }

    def status(self) -> dict[str, Any]:
        # A plane with several campaigns and no reference reports the plane,
        # not one campaign.  A bound campaign reports its own slice.
        if self._campaign_ref is None:
            working = self.store.list_campaigns(state="working")
            if len(working) > 1:
                return self._plane_status()
        campaign = self.campaign
        if campaign is None:
            return self._plane_status()
        key = campaign["key"]
        branch = self.config.get("target_branch") or self.config.get("main_branch")
        # Normalization is required, so it runs before every projection.
        try:
            dag_merge = self.normalize_dag()
        except SlicemeError as exc:
            dag_merge = {"merged": {}, "error": str(exc)}
        units = [self._project_unit(u, branch) for u in self._scoped_units()]
        candidates = self.store.list_candidates(campaign=key)
        waves = integrate.plan_waves(
            self.store,
            self.root,
            self.config,
            [c for c in candidates if c["status"] in {"prepared", "pending"}],
        )
        dag_waves, dag_waves_error = self._dag_waves(branch)
        return {
            "root": str(self.root),
            "main_branch": branch,
            "target_branch": branch,
            "campaign": campaign.get("name") or campaign["key"],
            "campaign_key": key,
            "worktree_branch": self.config.get("worktree_branch"),
            "feature_branch": branch,
            "default_branch": self.config.get("default_branch") or integrate.found_default_branch(self.root),
            "units": units,
            "candidates": candidates,
            "waves": [w.to_dict() for w in waves],
            "dag_waves": dag_waves,
            "dag_waves_error": dag_waves_error,
            "dag_merge": dag_merge,
            "executor": self.store.job_counts(campaign=key),
            "sandbox": self.sandbox_info(),
        }

    def _plane_status(self) -> dict[str, Any]:
        """A plane-level summary when no campaign is named."""
        cfg = self.plane_config
        campaigns = []
        for row in self.store.list_campaigns():
            candidates = self.store.list_candidates(campaign=row["key"])
            campaigns.append(
                {
                    "key": row["key"],
                    "target_branch": row["target_branch"],
                    "worktree_branch": row["worktree_branch"],
                    "unit_name": row["unit_name"],
                    "state": row["state"],
                    "candidates": len(candidates),
                    "landed": sum(1 for c in candidates if c["status"] == "landed"),
                }
            )
        return {
            "root": str(self.root),
            "plane": True,
            "default_branch": cfg.get("default_branch")
            or integrate.found_default_branch(self.root),
            "campaigns": campaigns,
        }

    # ------------------------------------------------------------------
    # Resume / session registry
    # ------------------------------------------------------------------
    def resume(self, *, plan_only: bool = False) -> dict[str, Any]:
        """Reconcile a suspended campaign and return its resume plan.

        Git and ``state.db`` always win over the adapter-written descriptor and
        ``state.json``.  The plan is a pure computation; unless *plan_only* the
        The plan is a pure computation over git plus ``state.db``.
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

        current_wave = state.get("current_wave")
        if current_wave is None:
            current_wave = descriptor.get("current_wave")
        if current_wave is None:
            current_wave = self._first_open_wave(state)

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

    @staticmethod
    def _first_open_wave(state: dict[str, Any]) -> int | None:
        waves = state.get("waves") or []
        for wave in waves:
            if str(wave.get("status")) != "done":
                return int(wave.get("index", 0))
        return None

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
            state = campaign.load_state(self.root, feature_branch)
            state_nodes = state.get("nodes") or {}
            dag = campaign.load_dag(self.root, feature_branch) or {}
            total = len([n for n in dag.get("nodes", []) if n.get("id")])
            done = sum(
                1
                for entry in state_nodes.values()
                if isinstance(entry, dict) and entry.get("status") == "done"
            )
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

    # ------------------------------------------------------------------
    # Attempts (per-subagent fidelity)
    # ------------------------------------------------------------------
    def begin_attempt(
        self,
        *,
        node: str,
        unit: str | None = None,
        attempt: int = 1,
        agent: str = "worker",
        started_at: float | None = None,
    ) -> dict[str, Any]:
        attempt_id = self.store.create_attempt(
            node=node,
            unit=unit,
            campaign=self.campaign_key(),
            attempt=attempt,
            agent=agent,
            started_at=started_at,
        )
        self.store.conn.commit()
        return self.store.get_attempt(attempt_id)  # type: ignore[return-value]

    def end_attempt(
        self, *, node: str, attempt: int | None = None, **fields: Any
    ) -> dict[str, Any] | None:
        """Finish the running attempt for *node* (optionally a specific attempt)."""
        row = self.store.find_running_attempt(
            node, attempt, campaign=self.campaign_key()
        )
        if row is None:
            return None
        result = self.store.finish_attempt(int(row["id"]), **fields)
        self.store.conn.commit()
        return result

    def attempts(
        self, *, node: str | None = None, statuses: list[str] | None = None
    ) -> dict[str, Any]:
        return {
            "attempts": self.store.list_attempts(
                node=node, statuses=statuses, campaign=self.campaign_key()
            )
        }

    def progress(self, *, node: str | None = None) -> dict[str, Any]:
        """Join attempts, jobs, heartbeats, and the DAG into one metric view.

        This is the durable half of ``docs/observability.md``: the time split,
        the tool rollup, the command rollup, and the verification cost.  It
        reads only ``state.db``, ``state.json``, and the heartbeat files, so a
        second terminal sees the same numbers.
        """
        campaign_row = self.campaign
        if campaign_row is None:
            raise SlicemeError("no campaign in this plane; run `sliceme start` first")
        key = str(campaign_row["key"])
        branch = self.config.get("target_branch") or self.config.get("main_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        state = campaign.load_state(self.root, branch) if branch else {"nodes": {}}
        nodes_dag = campaign.node_by_id(dag) if dag else {}
        waves, _ = self._dag_waves(branch)
        wave_of: dict[str, int] = {}
        for wave in waves:
            for member in wave.get("members", []):
                wave_of[str(member)] = int(wave.get("wave", 0))

        attempts = self.store.list_attempts(campaign=key)
        if node:
            attempts = [a for a in attempts if str(a.get("node")) == node]
        jobs = self.store.list_jobs(campaign=key)
        ts = now()

        totals: dict[str, Any] = {
            "nodes": 0,
            "done": 0,
            "running": 0,
            "pending": 0,
            "failed": 0,
            "attempts": len(attempts),
            "wall_seconds": 0.0,
            "tool_seconds": 0.0,
            "thinking_seconds": 0.0,
            "turns": 0,
            "tool_calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost": 0.0,
            "queue_wait_seconds": 0.0,
        }
        by_agent: dict[str, dict[str, Any]] = {}
        tool_seconds: dict[str, float] = {}
        tool_by_agent: dict[str, dict[str, float]] = {}
        tool_calls: dict[str, int] = {}
        command_totals: dict[str, dict[str, Any]] = {}
        earliest: float | None = None

        for attempt in attempts:
            duration = _metric_duration(attempt, ts)
            tool = _metric_number(attempt.get("tool_seconds"))
            thinking = max(0.0, duration - tool)
            role = str(attempt.get("agent") or "worker")
            turns = int(attempt.get("turns") or 0)
            calls = int(attempt.get("tool_calls") or 0)
            agent_totals = by_agent.setdefault(
                role,
                {
                    "attempts": 0,
                    "wall_seconds": 0.0,
                    "tool_seconds": 0.0,
                    "thinking_seconds": 0.0,
                    "turns": 0,
                    "tool_calls": 0,
                },
            )
            agent_totals["attempts"] += 1
            agent_totals["wall_seconds"] += duration
            agent_totals["tool_seconds"] += tool
            agent_totals["thinking_seconds"] += thinking
            agent_totals["turns"] += turns
            agent_totals["tool_calls"] += calls
            totals["wall_seconds"] += duration
            totals["tool_seconds"] += tool
            totals["thinking_seconds"] += thinking
            totals["turns"] += turns
            totals["tool_calls"] += calls
            totals["tokens_in"] += int(attempt.get("tokens_in") or 0)
            totals["tokens_out"] += int(attempt.get("tokens_out") or 0)
            totals["cost"] += _metric_number(attempt.get("cost"))
            started = _metric_number(attempt.get("started_at"))
            if started and (earliest is None or started < earliest):
                earliest = started
            for tool_name, seconds in _metric_object(attempt.get("tool_durations")).items():
                value = _metric_number(seconds)
                tool_seconds[tool_name] = tool_seconds.get(tool_name, 0.0) + value
                named = tool_by_agent.setdefault(tool_name, {})
                named[role] = named.get(role, 0.0) + value
            for tool_name, count in _metric_object(attempt.get("tools")).items():
                tool_calls[tool_name] = tool_calls.get(tool_name, 0) + int(count or 0)
            for entry in _metric_list(attempt.get("slowest_commands")):
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("command") or "?")
                item = command_totals.setdefault(
                    name,
                    {"command": name, "tool": entry.get("tool"), "seconds": 0.0, "calls": 0},
                )
                item["seconds"] += _metric_number(entry.get("seconds"))
                item["calls"] += int(entry.get("calls") or 0)

        passed = failed = 0
        executor_seconds = 0.0
        for job in jobs:
            requested = job.get("requested_at")
            started = job.get("started_at")
            if requested and started:
                totals["queue_wait_seconds"] += max(0.0, float(started) - float(requested))
            executor_seconds += _metric_number(job.get("duration"))
            if job.get("status") == "passed":
                passed += 1
            elif job.get("status") in {"failed", "error", "cancelled"}:
                failed += 1
        terminal = passed + failed

        tools = [
            {
                "tool": tool_name,
                "seconds": round(seconds, 3),
                "calls": tool_calls.get(tool_name, 0),
                "avg": (
                    round(seconds / tool_calls[tool_name], 3)
                    if tool_calls.get(tool_name)
                    else None
                ),
                "by_agent": {
                    name: round(value, 3)
                    for name, value in tool_by_agent.get(tool_name, {}).items()
                },
            }
            for tool_name, seconds in tool_seconds.items()
        ]
        tools.sort(key=lambda item: item["seconds"], reverse=True)
        commands = sorted(
            command_totals.values(), key=lambda item: item["seconds"], reverse=True
        )[:20]
        for item in commands:
            item["seconds"] = round(item["seconds"], 3)

        latest: dict[str, dict[str, Any]] = {}
        for attempt in attempts:
            latest[str(attempt.get("node"))] = attempt  # ascending id: last wins
        selected = [node] if node else list(nodes_dag)
        node_rows: list[dict[str, Any]] = []
        for node_id in selected:
            entry = nodes_dag.get(node_id) or {}
            status = campaign.node_status(state, node_id)
            if status in totals:
                totals[status] += 1
            attempt_row = latest.get(node_id)
            duration = _metric_duration(attempt_row, ts) if attempt_row else 0.0
            tool = _metric_number(attempt_row.get("tool_seconds")) if attempt_row else 0.0
            heartbeat = _read_heartbeat(campaign.heartbeat_path(self.root, branch, node_id))
            updated = _metric_number(heartbeat.get("updated_at"))
            age = max(0.0, ts - updated) if updated else None
            node_rows.append(
                {
                    "id": node_id,
                    "label": entry.get("label"),
                    "wave": wave_of.get(node_id),
                    "status": status,
                    "attempt": int(attempt_row.get("attempt") or 0) if attempt_row else 0,
                    "wall_seconds": round(duration, 3),
                    "tool_seconds": round(tool, 3),
                    "thinking_seconds": round(max(0.0, duration - tool), 3),
                    "turns": int(attempt_row.get("turns") or 0) if attempt_row else 0,
                    "tool_calls": int(attempt_row.get("tool_calls") or 0) if attempt_row else 0,
                    "tokens_in": int(attempt_row.get("tokens_in") or 0) if attempt_row else 0,
                    "tokens_out": int(attempt_row.get("tokens_out") or 0) if attempt_row else 0,
                    "cost": round(_metric_number(attempt_row.get("cost")) if attempt_row else 0.0, 3),
                    "heartbeat_age": round(age, 3) if age is not None else None,
                    "stalled": bool(
                        status == "running"
                        and age is not None
                        and age > STALLED_AFTER_SECONDS
                    ),
                }
            )

        totals["nodes"] = len(selected)
        start_ref = earliest or _metric_number(campaign_row.get("created_at")) or ts
        totals["elapsed"] = round(max(0.0, ts - start_ref), 3)
        rounded = {
            name: round(value, 3) if isinstance(value, float) else value
            for name, value in totals.items()
        }
        by_agent_rounded = {
            role: {
                name: round(value, 3) if isinstance(value, float) else value
                for name, value in data.items()
            }
            for role, data in by_agent.items()
        }
        return {
            "campaign": campaign_row.get("name") or key,
            "campaign_key": key,
            "now": ts,
            "totals": rounded,
            "by_agent": by_agent_rounded,
            "tools": tools,
            "commands": commands,
            "verification": {
                "executor_seconds": round(executor_seconds, 3),
                "jobs": len(jobs),
                "passed": passed,
                "failed": failed,
                "pass_rate": round(passed / terminal, 4) if terminal else None,
            },
            "nodes": node_rows,
            "executor": self.store.job_counts(campaign=key),
        }

    # ------------------------------------------------------------------
    # Local review (docs/review.md)
    # ------------------------------------------------------------------
    def review_snapshot(self, *, commit: str | None = None) -> dict[str, Any]:
        from .review import packet

        return packet.build_packet(self, commit=commit)

    def review_diff(self, commit: str | None, path: str) -> dict[str, Any]:
        from .review import packet

        return packet.file_lines(self, commit, path)

    def review_file(self, commit: str | None, path: str) -> dict[str, Any]:
        from .review import packet

        return packet.file_content(self, commit, path)

    def review_comment(
        self,
        *,
        body: str,
        commit: str | None = None,
        file: str | None = None,
        side: str | None = None,
        line: int | None = None,
        line_end: int | None = None,
        node: str | None = None,
    ) -> dict[str, Any]:
        """Record one comment against a commit (or the report), file, and line."""
        from .review import packet

        if not (body or "").strip():
            raise SlicemeError("a comment needs a body")
        if side and side not in {"old", "new"}:
            raise SlicemeError("comment side must be one of: old, new")
        comment = self.store.add_comment(
            branch_key=packet.campaign_branch_key(self),
            body=body.strip(),
            commit_hash=commit,
            file=file,
            side=side,
            line=line,
            line_end=line_end,
            node=node,
        )
        self.store.conn.commit()
        self._log_review_event(
            "comment",
            {"comment": int(comment["id"]), "commit": commit, "file": file},
        )
        return comment

    def review_decision(
        self,
        *,
        action: str,
        commit: str | None = None,
        all_commits: bool = False,
        actor: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Append one decision for one commit, or approve every commit at once."""
        from .review import packet

        if action not in {"approve", "request_changes", "override"}:
            raise SlicemeError(
                "decision must be one of: approve, request_changes, override"
            )
        branch_key = packet.campaign_branch_key(self)
        if all_commits:
            if action != "approve":
                raise SlicemeError("only approve can apply to all commits")
            return self._approve_all(branch_key, actor=actor, note=note)
        if commit is None and action != "override":
            raise SlicemeError("a decision needs --commit <sha>, --all, or an override")
        if action == "request_changes":
            open_comments = self.store.list_comments(
                branch_key=branch_key, statuses=["open"]
            )
            if not (note or "").strip() and not open_comments:
                raise SlicemeError(
                    "request_changes needs a note or at least one open comment"
                )
        if action == "override" and not (note or "").strip():
            raise SlicemeError("override needs a note")
        decision = self.store.add_review_decision(
            branch_key=branch_key,
            commit_hash=commit,
            action=action,
            actor=actor,
            note=note,
        )
        self.store.conn.commit()
        self._log_review_event(
            "decision",
            {"decision": int(decision["id"]), "action": action, "commit": commit},
        )
        return decision

    def _approve_all(
        self, branch_key: str, *, actor: str | None, note: str | None
    ) -> dict[str, Any]:
        approved = [
            self.store.add_review_decision(
                branch_key=branch_key,
                commit_hash=commit,
                action="approve",
                actor=actor,
                note=note,
            )
            for commit in self.unapproved_commits()
        ]
        self.store.conn.commit()
        self._log_review_event(
            "decision", {"action": "approve", "all": True, "count": len(approved)}
        )
        return {"action": "approve", "all": True, "approved": len(approved)}

    def review_poll(self) -> dict[str, Any]:
        """Open comments plus the approval state, for the pi relay."""
        from .review import packet

        branch_key = packet.campaign_branch_key(self)
        unapproved = self.unapproved_commits()
        return {
            "branch_key": branch_key,
            "comments": self.store.list_comments(
                branch_key=branch_key, statuses=["open"]
            ),
            "unapproved": unapproved,
            "all_approved": not unapproved,
        }

    def review_ack(self, comment_id: int) -> dict[str, Any]:
        """Mark one comment delivered to the coordinator session."""
        comment = self.store.get_comment(int(comment_id))
        if comment is None:
            raise SlicemeError(f"unknown comment: {comment_id}")
        updated = self.store.set_comment_status(int(comment_id), "delivered")
        self.store.conn.commit()
        return updated  # type: ignore[return-value]

    def unapproved_commits(self) -> list[str]:
        """Campaign commits with no valid, unconsumed approval."""
        from .review import packet

        branch_key = packet.campaign_branch_key(self)
        decisions = self.store.latest_decisions_by_commit(branch_key)
        return [
            commit
            for commit in packet.review_commits(self)
            if not self._is_approved(decisions.get(commit))
        ]

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
        """Refuse delivery unless every commit is approved (or an override).

        Returns the override decision when one admitted delivery, else ``None``.
        """
        from .review import packet

        branch_key = packet.campaign_branch_key(self)
        commits = commits if commits is not None else packet.campaign_commits(self)
        decisions = self.store.latest_decisions_by_commit(branch_key)
        unapproved = [c for c in commits if not self._is_approved(decisions.get(c))]
        if not unapproved:
            return None
        override = self.store.latest_review_decision(branch_key, None)
        if (
            override
            and override.get("action") == "override"
            and (override.get("note") or "").strip()
        ):
            return override
        listing = ", ".join(commit[:7] for commit in unapproved)
        raise SlicemeError(
            f"not-approved: {listing} not approved; open `sliceme review` and approve them"
        )

    def consume_approvals(self, commits: list[str] | None = None) -> None:
        """Mark the approvals for *commits* consumed after a landed merge."""
        from .review import packet

        branch_key = packet.campaign_branch_key(self)
        commits = commits if commits is not None else packet.campaign_commits(self)
        decisions = self.store.latest_decisions_by_commit(branch_key)
        ids = [
            int(decisions[commit]["id"])
            for commit in commits
            if commit in decisions and self._is_approved(decisions[commit])
        ]
        self.store.consume_review_decisions(ids)
        self.store.conn.commit()

    def _log_review_event(self, kind: str, data: dict[str, Any]) -> None:
        """Append one review audit line to ``.sliceme/<branch-key>.events.jsonl``."""
        import json

        from .util import state_dir

        branch = self.config.get("target_branch") or self.config.get("main_branch") or "main"
        path = state_dir(self.root) / f"{campaign.branch_key(branch)}.events.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"kind": f"review.{kind}", "data": data}) + "\n")
        except OSError:
            pass

    def normalize_dag(self) -> dict[str, Any]:
        """Contract same-ownership DAG chains before the wave projection.

        This runs before every projection, so a campaign never schedules an
        un-normalized DAG.  A node with recorded progress stays separate.
        """
        branch = self.config.get("target_branch") or self.config.get("main_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        if not dag or not dag.get("nodes"):
            return {"merged": {}}

        # Keep any node that already has progress out of the merge.
        nodes = [dict(node) for node in dag["nodes"]]
        statuses = campaign.load_state(self.root, branch).get("nodes") or {}
        recorded = {
            str(c["node"])
            for c in self.store.list_candidates(campaign=self.campaign_key())
            if c.get("node")
        }
        for node in nodes:
            nid = str(node.get("id"))
            entry = statuses.get(nid)
            progressed = isinstance(entry, dict) and entry.get("status") not in (
                None,
                "pending",
            )
            if nid in recorded or progressed:
                node["no_merge"] = True

        wave_size = int(dag.get("concurrency") or DEFAULT_WAVE_SIZE)
        before_waves = plan_dag_waves(list(dag["nodes"]), wave_size=wave_size)
        merged_nodes, merge_map = merge_same_own_nodes(nodes)
        after_waves = plan_dag_waves(merged_nodes, wave_size=wave_size)
        if merge_map:
            write_json(
                campaign.dag_path(self.root, branch), {**dag, "nodes": merged_nodes}
            )
        return {
            "path": str(campaign.dag_path(self.root, branch)),
            "merged": merge_map,
            "before_nodes": len(nodes),
            "after_nodes": len(merged_nodes),
            "before_waves": len(before_waves),
            "after_waves": len(after_waves),
            "waves": [wave.to_dict() for wave in after_waves],
        }

    def _dag_waves(self, branch: str | None) -> tuple[list[dict[str, Any]], str | None]:
        """Compute the scheduler's wave projection of the campaign DAG.

        The DAG is the only authored schedule; waves are derived here from
        ``owns``/``depends_on`` and the campaign's ``concurrency`` cap.  A
        malformed or cyclic DAG is reported rather than crashing ``status``.
        """
        if not branch:
            return [], None
        dag = campaign.load_dag(self.root, branch)
        if not dag or not dag.get("nodes"):
            return [], None
        wave_size = int(dag.get("concurrency") or DEFAULT_WAVE_SIZE)
        try:
            validate_dag(list(dag["nodes"]))
            planned = plan_dag_waves(list(dag["nodes"]), wave_size=wave_size)
        except SlicemeError as exc:
            return [], str(exc)
        return [w.to_dict() for w in planned], None

    def _project_unit(self, unit: dict[str, Any], branch: str | None) -> dict[str, Any]:
        """Add the campaign columns the dashboard needs (§6.3).

        ``node``/``log`` come from the campaign layout; ``candidate`` and
        ``verification`` are the unit's latest candidate row and the newest
        check job for its commit.
        """
        projected = dict(unit)
        unit_id = int(unit["id"])
        candidates = [
            c
            for c in self.store.list_candidates(campaign=self.campaign_key())
            if int(c["unit_id"]) == unit_id
        ]
        latest = candidates[-1] if candidates else None
        verification = (
            self.store.latest_job_for_commit(str(latest["head_commit"]))
            if latest is not None
            else None
        )
        projected["node"] = unit["name"]
        projected["log"] = str(
            campaign.worker_log_path(self.root, branch or "main", unit["name"])
        )
        projected["candidate"] = int(latest["id"]) if latest is not None else None
        projected["verification"] = verification
        return projected

    def deliver(
        self,
        *,
        target: str | None = None,
        source: str | None = None,
        no_ff: bool = True,
        cleanup: str = "none",
        run_checks_flag: bool = True,
    ) -> dict[str, Any]:
        """Merge the campaign worktree into the target branch (agent-callable).

        This is the single, end-of-campaign merge.  The target branch is never
        the default branch and there is no override.
        """
        if cleanup not in {"none", "worktrees", "all"}:
            raise SlicemeError("cleanup must be one of: none, worktrees, all")
        from .review import packet

        target_branch = target or self.config.get("target_branch") or self.config.get("main_branch")
        source_branch = source or self.config.get("worktree_branch")
        commits = packet.campaign_commits(self)
        with _delivery_lock(self.root):
            override = self.require_all_approved(commits)
            results = integrate.deliver(
                self.store,
                self.root,
                self.config,
                campaign=self.campaign_key(),
                target=target,
                source=source,
                no_ff=no_ff,
                run_checks_flag=run_checks_flag,
            )
            if results and all(result.status == "landed" for result in results):
                self.consume_approvals(commits)
                if override:
                    self.store.consume_review_decisions([int(override["id"])])
                    self.store.conn.commit()
                key = self.campaign_key()
                if key:
                    self.store.set_campaign_state(key, "delivered")
                    self.store.conn.commit()
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

    def remove_campaign_artifacts(self, *, keep_report: bool = True) -> list[str]:
        """Delete ``<branch-key>`` dag/state/worker logs (report kept by default)."""
        branch = self.config.get("main_branch") or "main"
        removed: list[str] = []
        paths = [
            campaign.dag_path(self.root, branch),
            campaign.state_path(self.root, branch),
        ]
        removed.extend(self._remove_files(paths))
        for log in sorted(self.root.glob(f".sliceme/{campaign.branch_key(branch)}.worker_*.log")):
            removed.extend(self._remove_files([log]))
        if not keep_report:
            removed.extend(self._remove_files([campaign.report_path(self.root, branch)]))
        return removed

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

    def gc(self) -> dict[str, Any]:
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
        from .util import rmtree

        scratch = self.root / ".sliceme" / "scratch"
        rmtree(scratch)
        pruned_reviews = self._prune_reviews()
        self.store.conn.commit()
        return {
            "removed_worktrees": removed,
            "pruned_branches": pruned_branches,
            "pruned_reviews": pruned_reviews,
        }

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


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
@contextmanager
def _campaign_lock(root: Path):
    """The campaign-creation lock, separate from the executor lock."""
    import fcntl

    from .util import state_dir

    path = state_dir(root) / "campaigns.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
@contextmanager
def _delivery_lock(root: Path):
    """The plane delivery lock, separate from ``executor.lock`` (POSIX only)."""
    import fcntl

    from .util import state_dir

    path = state_dir(root) / "review.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _campaign_has_work(store: Store, root: Path, campaign_row: dict[str, Any]) -> bool:
    """Whether a campaign already recorded a DAG, a candidate, or a unit."""
    if campaign.dag_path(root, str(campaign_row["target_branch"])).exists():
        return True
    if store.list_candidates(campaign=str(campaign_row["key"])):
        return True
    return store.get_unit_by_campaign(str(campaign_row["key"])) is not None


def _owners_of(path: str, owners: dict[str, list[str]]) -> list[str]:
    return [node_id for node_id, owns in owners.items() if path_within_owns(path, owns)]


def _describe_violation(
    status: str,
    path: str,
    old: str | None,
    new_owners: list[str],
    old_owners: list[str],
) -> str:
    if old:
        return f"{status} {old} -> {path} spans nodes {old_owners}/{new_owners}"
    if not new_owners:
        return f"{status} {path} is outside every wave node"
    return f"{status} {path} is claimed by {new_owners}"


def _changed_entries(
    worktree: Path, base: str | None = None
) -> list[tuple[str, str, str | None]]:
    """Staged changes as ``(status, path, old_path)``, rename-aware.

    With no *base* the diff is against ``HEAD`` (the last recorded wave).
    """
    args = ["diff", "--cached", "--name-status", "-M"]
    if base:
        args.append(base)
    result = gitutil.git(worktree, *args, check=False)
    if not result.ok:
        return []
    entries: list[tuple[str, str, str | None]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status[:1] in {"R", "C"} and len(parts) >= 3:
            entries.append((status, parts[2], parts[1]))
        else:
            entries.append((status, parts[1], None))
    return entries


def _resolve_target_branch(
    root: Path,
    *,
    target_branch: str | None,
    target_mode: str | None,
    base: str | None,
) -> str:
    """Resolve the campaign target branch from the user's choice.

    ``current`` adopts the checked-out branch; ``existing`` requires the named
    branch to exist; ``new`` creates it from *base* (default ``HEAD``).  A bare
    target name with no mode is treated as an existing branch.
    """
    mode = (target_mode or "").strip().lower()
    if mode not in {"", "current", "existing", "new"}:
        raise SlicemeError("target_mode must be one of: current, existing, new")
    if mode == "new":
        if not target_branch:
            raise SlicemeError("target_mode 'new' requires a target branch name")
        if gitutil.branch_exists(root, target_branch):
            raise SlicemeError(f"branch '{target_branch}' already exists")
        from_commit = base or gitutil.rev_parse(root, "HEAD")
        gitutil.create_branch(root, target_branch, from_commit)
        return target_branch
    if target_branch:
        if not gitutil.branch_exists(root, target_branch):
            raise SlicemeError(f"branch '{target_branch}' does not exist")
        return target_branch
    current = gitutil.current_branch(root)
    if not current:
        raise SlicemeError(
            "not on a branch; create or check out the campaign branch before start"
        )
    return current


def _default_worktree_branch(root: Path, target: str) -> str:
    """A stable, unique accumulation branch for the campaign worktree."""
    base = f"sliceme/{slugify(target or 'campaign', 32)}"
    branch = base
    counter = 2
    while gitutil.branch_exists(root, branch):
        branch = f"{base}-{counter}"
        counter += 1
    return branch


def _unique_branch(root: Path, name: str) -> str:
    base = f"sliceme/{slugify(name, 32)}"
    branch = base
    counter = 2
    while gitutil.branch_exists(root, branch):
        branch = f"{base}-{counter}"
        counter += 1
    return branch


def _unique_worktree(base_dir: Path, name: str) -> Path:
    candidate = base_dir / slugify(name, 32)
    path = candidate
    counter = 2
    while path.exists():
        path = candidate.with_name(candidate.name + f"-{counter}")
        counter += 1
    return path


def _ensure_gitignore(root: Path) -> None:
    """Ignore ``.sliceme/`` via the repo-local exclude file.

    Using ``.git/info/exclude`` (shared by all worktrees) keeps the main
    worktree clean, unlike creating an untracked ``.gitignore``.
    """
    entry = ".sliceme/"
    res = gitutil.git(root, "rev-parse", "--git-common-dir", check=False)
    git_dir = Path(res.stdout.strip()) if res.ok else root / ".git"
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    content = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if entry in {line.strip() for line in content.splitlines()}:
        return
    if content and not content.endswith("\n"):
        content += "\n"
    exclude.write_text(content + entry + "\n", encoding="utf-8")
