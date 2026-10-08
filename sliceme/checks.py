"""One synchronous combined-tree check runner plus a persistent cache.

``checks.py`` replaces the executor queue.  A wave records its nodes as commits
on the campaign worktree, then one check set runs over that combined tree in a
scratch worktree.  The runner does not hold a lease and does not queue work: it
runs one check set and returns the row.

The ``checks`` table is a cache, not a queue.  Every row is a terminal verdict
(``passed``, ``failed``, or ``error``).  A run whose fingerprint already has a
terminal row reuses it, so a resumed campaign re-verifies a node from the cache
instead of a full check, and delivery reads the row as evidence.

The fingerprint pins ``(tree, command vector, toolchain, policy, sandbox,
checks, source)`` (``docs/reference.md`` §4), so a change to any of them
invalidates the cached verdict.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import gitutil
from .sandbox import Sandbox
from .sandbox import require_sandbox as _require_sandbox
from .store import CHECK_VERDICTS, Store
from .util import SlicemeError
from .verifier import (
    CheckSpec,
    acceptance_checks,
    compute_fingerprint,
    run_checks,
    select_checks,
)

#: Bumped when check semantics change so cached fingerprints invalidate.
CHECKS_VERSION = "1"

__all__ = ["CHECKS_VERSION", "CheckRunner"]


class CheckRunner:
    """Run one check set over a commit and cache the terminal verdict."""

    def __init__(
        self,
        root: Path,
        store: Store,
        config: dict[str, Any],
        *,
        dag: dict[str, Any] | None = None,
        campaign: str | None = None,
    ):
        self.root = Path(root)
        self.store = store
        self.config = config
        self.dag = dag
        self.campaign = campaign

    # -- configuration --------------------------------------------------
    def require_sandbox(
        self, *, gpu_required: bool = False, override: str | None = None
    ) -> Sandbox:
        """Resolve and validate the sandbox gate (fail closed when required)."""
        return _require_sandbox(
            self.dag,
            self.config,
            root=self.root,
            gpu_required=gpu_required,
            override=override,
        )

    # -- the one check run ----------------------------------------------
    def run(
        self,
        *,
        source: str,
        commit: str | None = None,
        commands: list[str] | None = None,
        checks: list[CheckSpec] | None = None,
        only: list[str] | None = None,
        sandbox: str | None = None,
        gpu: str = "none",
        wave: int | None = None,
        timeout: int = 3600,
    ) -> dict[str, Any]:
        """Run one check set at *commit*, or return the cached terminal row.

        ``commands`` are bare acceptance command strings; ``checks`` passes a
        caller's :class:`~sliceme.verifier.CheckSpec` vector instead.  ``only``
        keeps just the named checks, so a re-verify can run one command.

        The returned row carries every stored column plus ``cached``: true when
        a terminal verdict for the fingerprint already existed.
        """
        if not source:
            raise SlicemeError("check requires a source (e.g. wave:0, deliver)")
        if not commit:
            raise SlicemeError("check requires a commit")
        if checks is None and not commands:
            raise SlicemeError("check requires at least one check")
        commit_sha = gitutil.rev_parse(self.root, commit)
        profile = self.require_sandbox(gpu_required=(gpu != "none"), override=sandbox)
        resolved = (
            list(checks)
            if checks is not None
            else acceptance_checks(list(commands or []), timeout=int(timeout))
        )
        selected = select_checks(resolved, only)
        if only and not selected:
            raise SlicemeError(
                "check --only matched no check: " + ", ".join(map(str, only))
            )
        check_specs = [
            {
                "name": c.name,
                "command": c.command,
                "required": c.required,
                "timeout": c.timeout,
            }
            for c in selected
        ]
        fingerprint = compute_fingerprint(
            self.root,
            self.config,
            commit_sha,
            checks=selected,
            source=source,
            sandbox_digest=profile.digest(),
            checks_digest=CHECKS_VERSION,
        )
        cached = self.store.find_check(
            fingerprint.fingerprint, statuses=CHECK_VERDICTS
        )
        if cached is not None:
            return {**cached, "cached": True}

        row: dict[str, Any] = {
            "fingerprint": fingerprint.fingerprint,
            "source": source,
            "commit_ref": commit_sha,
            "commands": [c.command for c in selected],
            "checks": check_specs,
            "wave": wave,
            "campaign": self.campaign,
            "tree": fingerprint.tree,
            "sandbox": profile.to_dict(),
            "sandbox_digest": profile.digest(),
            "gpu": gpu,
        }
        # One synchronous run.  Any failure becomes an error verdict, so a bad
        # command never leaves the row half written.
        try:
            status, results, duration = run_checks(
                self.root,
                self.config,
                commit_sha,
                checks=selected,
                sandbox=profile,
                tier=gpu,
            )
            row.update(
                status=status,
                duration=duration,
                exit_code=_exit_code(results),
                output=_format_checks(results),
                results=json.dumps([result.to_dict() for result in results]),
            )
        except Exception as exc:  # noqa: BLE001 - surface any failure as a verdict
            row.update(status="error", error=str(exc))
        check_id = self.store.create_check(**row)
        self.store.conn.commit()
        return {**self.store.get_check(check_id), "cached": False}


def _exit_code(results: list[Any]) -> int:
    for result in results:
        if result.returncode:
            return int(result.returncode)
    return 0


def _format_checks(results: list[Any]) -> str:
    lines: list[str] = []
    for result in results:
        lines.append(f"[{result.status}] {result.name}: {result.command}")
        if result.output:
            lines.append(result.output[-2000:])
    return "\n".join(lines) or "(no checks run)"
