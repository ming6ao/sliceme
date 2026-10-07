"""The single verification executor: a sandboxed command queue.

Multiple verifiers delegate to one executor.  A verifier *submits* a check job
(a command vector over a commit); the executor *runs* jobs one at a time behind
a process-wide ``flock``, in a sandbox, on a detached scratch worktree, and
records the result.  Requesters then ``wait`` for the terminal status.

Design (see ``docs/guide.md`` / ``docs/reference.md``):

* **One runner.** ``run``/``drain`` hold an exclusive lock, so no two check
  vectors run concurrently — protecting the GPU and shared resources.
* **Dedupe by fingerprint.** A submit whose ``(tree, commands, toolchain,
  policy, sandbox, source)`` fingerprint already reached a terminal verdict
  (``passed``, ``failed``, or ``error``) is returned cached; the commands are
  not re-run (DEC-3: a cached pass and a failed job each skip the verifier).
* **A batch, one drain.** A submit takes one commit or a list of commits and
  enqueues one job per commit; a single ``drain`` then verifies a whole wave.
* **One command on demand.** A submit may pass ``only`` to keep just the named
  commands, so a re-verify can re-run a single command.
* **Sandboxed.** ``sliceme.sandbox`` resolves the isolation profile; its digest
  is part of the fingerprint, so a stricter sandbox invalidates a verdict.
* **Crash-safe.** A ``running`` job whose lease expired is reset to ``queued``
  before a drain, matching the plane's git/DB-wins recovery rule.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import gitutil
from .sandbox import Sandbox, coerce_sandbox, resolve_sandbox
from .sandbox import require_sandbox as _require_sandbox
from .store import Store
from .util import SlicemeError, now, state_dir
from .verifier import (
    CheckSpec,
    acceptance_checks,
    compute_fingerprint,
    run_checks,
    select_checks,
)

#: Bumped when executor semantics change so cached fingerprints invalidate.
EXECUTOR_VERSION = "1"

#: A ``running`` job silent for longer than this is treated as orphaned.
DEFAULT_LEASE_SECONDS = 900.0

#: Terminal job statuses.
TERMINAL = ("passed", "failed", "error", "cancelled")

#: Terminal statuses a submit may reuse from the cache (DEC-3).  A cached pass
#: and a failed/errored run both stand as verdicts; a ``cancelled`` job does not,
#: so a re-submit runs it again.
CACHEABLE = ("passed", "failed", "error")


class Executor:
    """Queue and run sandboxed verification command vectors."""

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

    # -- locking --------------------------------------------------------
    @contextmanager
    def lock(self) -> Iterator[bool]:
        """Hold the single-executor lock for the duration of a run/drain."""
        path = state_dir(self.root) / "executor.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX fallback
            yield False
            return
        handle = open(path, "w", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield True
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()

    # -- configuration --------------------------------------------------
    def sandbox(self, override: str | None = None) -> Sandbox:
        return resolve_sandbox(self.dag, self.config, root=self.root, override=override)

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

    # -- queue ----------------------------------------------------------
    def submit(
        self,
        *,
        source: str | None,
        commit: str | list[str] | None = None,
        commits: list[str] | None = None,
        commands: list[str] | None = None,
        checks: list[CheckSpec] | None = None,
        only: list[str] | None = None,
        sandbox: str | None = None,
        gpu: str = "none",
        wave: int | None = None,
        requester: str | None = None,
        priority: int = 0,
        timeout: int = 3600,
    ) -> dict[str, Any]:
        """Enqueue one check job per commit, or return cached terminal results.

        A batch of commits (``commit`` as one ref or a list, plus ``commits``)
        becomes one job per commit sharing the command vector, so a single
        ``drain`` verifies a whole wave.  ``only`` keeps just the named commands
        (matched by check name or command text), so a re-verify can run one
        command.  A fingerprint that already reached a terminal verdict is
        returned cached and the commands are not re-run (DEC-3).

        ``checks`` passes a caller's :class:`~sliceme.verifier.CheckSpec` vector
        (for example the plane's ``checks_from_config``) instead of deriving
        names, ``required``, and ``timeout`` from bare command strings.  The
        specs are persisted on the job so the drain honors the caller's checks,
        not a default.

        Returns ``{"jobs": [<row>, ...], "job": <first row>, "cached": bool}``.
        ``cached`` is true only when every requested commit was served from the
        cache; a single commit still reads as ``result["job"]``.
        """
        if not source:
            raise SlicemeError("exec submit requires --source (e.g. node:w1, wave:0)")
        refs = _commit_refs(commit, commits)
        if not refs:
            raise SlicemeError("exec submit requires --commit")
        if checks is None and not commands:
            raise SlicemeError("exec submit requires at least one --command")

        profile = self.require_sandbox(gpu_required=(gpu != "none"), override=sandbox)
        resolved = (
            list(checks)
            if checks is not None
            else acceptance_checks(list(commands or []), timeout=int(timeout))
        )
        selected = select_checks(resolved, only)
        if not selected:
            raise SlicemeError(
                "exec submit --only matched no command: " + ", ".join(map(str, only or []))
            )
        command_vector = [c.command for c in selected]
        check_specs = [
            {
                "name": c.name,
                "command": c.command,
                "required": c.required,
                "timeout": c.timeout,
            }
            for c in selected
        ]

        jobs: list[dict[str, Any]] = []
        cached = True
        for ref in refs:
            commit_sha = gitutil.rev_parse(self.root, ref)
            fingerprint = compute_fingerprint(
                self.root,
                self.config,
                commit_sha,
                checks=selected,
                source=source,
                sandbox_digest=profile.digest(),
                executor_digest=EXECUTOR_VERSION,
            )
            hit = self._cached_job(fingerprint.fingerprint)
            if hit is not None:
                jobs.append(hit)
                continue
            cached = False
            job_id = self.store.create_job(
                wave=wave,
                campaign=self.campaign,
                requester=requester,
                source=source,
                commit_ref=commit_sha,
                tree=gitutil.tree_of(self.root, commit_sha),
                commands=list(command_vector),
                checks=check_specs,
                sandbox=profile.to_dict(),
                sandbox_digest=profile.digest(),
                gpu=gpu,
                priority=int(priority),
                fingerprint=fingerprint.fingerprint,
                timeout=int(timeout),
            )
            jobs.append(self.store.get_job(job_id))
        return {"jobs": jobs, "job": jobs[0], "cached": cached}

    def _cached_job(self, fingerprint: str) -> dict[str, Any] | None:
        """The newest cached terminal job for *fingerprint*, for dedupe (DEC-3)."""
        placeholders = ",".join("?" for _ in CACHEABLE)
        row = self.store.conn.execute(
            f"SELECT * FROM jobs WHERE fingerprint=? AND status IN ({placeholders})"
            " ORDER BY id DESC LIMIT 1",
            (fingerprint, *CACHEABLE),
        ).fetchone()
        return dict(row) if row is not None else None

    def run_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Run one claimed job in the resolved sandbox and record the result."""
        job_id = int(job["id"])
        self.store.update_job(
            job_id, status="running", started_at=now(), runner_pid=os.getpid()
        )
        commands = json.loads(job["commands"])
        stored = json.loads(job["checks"]) if job.get("checks") else None
        profile = (
            coerce_sandbox(json.loads(job["sandbox"]))
            if job.get("sandbox")
            else self.sandbox()
        )
        try:
            checks = (
                [CheckSpec.from_config(item) for item in stored]
                if stored
                else acceptance_checks(commands, timeout=int(job.get("timeout") or 3600))
            )
            status, results, duration = run_checks(
                self.root,
                self.config,
                str(job["commit_ref"]),
                checks=checks,
                sandbox=profile,
                tier=str(job.get("gpu") or "none"),
            )
            self.store.update_job(
                job_id,
                status=status,
                finished_at=now(),
                duration=duration,
                exit_code=_exit_code(results),
                output=_format_checks(results),
                results=json.dumps([result.to_dict() for result in results]),
                error=None,
            )
        except Exception as exc:  # noqa: BLE001 - surface any failure as a job error
            self.store.update_job(
                job_id, status="error", finished_at=now(), error=str(exc)
            )
        self.store.conn.commit()
        return self.store.get_job(job_id)

    def run_next(self, *, lease: float = DEFAULT_LEASE_SECONDS) -> dict[str, Any] | None:
        """Claim and run the highest-priority queued job (single runner)."""
        self.recover_orphans(lease=lease)
        with self.lock():
            return self._claim_and_run()

    def drain(
        self, *, limit: int | None = None, lease: float = DEFAULT_LEASE_SECONDS
    ) -> list[dict[str, Any]]:
        """Drain the queue, one job at a time, under a single lock."""
        self.recover_orphans(lease=lease)
        done: list[dict[str, Any]] = []
        with self.lock():
            while limit is None or len(done) < limit:
                job = self._claim_and_run()
                if job is None:
                    break
                done.append(job)
        return done

    def _claim_and_run(self) -> dict[str, Any] | None:
        job = self.store.claim_next_job(runner_pid=os.getpid())
        if job is None:
            return None
        return self.run_job(job)

    def wait(
        self, job_id: int | str, *, timeout: float = 600.0, interval: float = 0.05
    ) -> dict[str, Any]:
        """Block until *job_id* reaches a terminal status (or the timeout)."""
        deadline = time.time() + timeout if timeout and timeout > 0 else None
        while True:
            job = self.store.get_job(job_id)
            if job is None:
                raise SlicemeError(f"unknown job: {job_id}")
            if job["status"] in TERMINAL:
                return job
            if deadline is not None and time.time() >= deadline:
                return job
            time.sleep(interval)

    def cancel(self, job_id: int | str) -> dict[str, Any]:
        """Cancel a queued job.  A running job cannot be cancelled in Phase 1."""
        job = self.store.get_job(job_id)
        if job is None:
            raise SlicemeError(f"unknown job: {job_id}")
        if job["status"] == "queued":
            self.store.update_job(int(job["id"]), status="cancelled", finished_at=now())
            self.store.conn.commit()
        return self.store.get_job(int(job["id"]))

    def recover_orphans(self, *, lease: float = DEFAULT_LEASE_SECONDS) -> int:
        """Return expired ``running`` jobs to the queue (crash recovery)."""
        return self.store.recover_orphan_jobs(cutoff=now() - lease)

    def status(self, *, limit: int = 50) -> dict[str, Any]:
        return {
            "counts": self.store.job_counts(),
            "queued": self.store.list_jobs(statuses=["queued"], limit=limit),
            "running": self.store.list_jobs(statuses=["running"], limit=limit),
            "recent": self.store.list_jobs(limit=limit),
        }


def _commit_refs(
    commit: str | list[str] | None, commits: list[str] | None
) -> list[str]:
    """Normalize the commit batch, dropping blanks and duplicates in order."""
    raw: list[Any] = list(commits or [])
    if isinstance(commit, str):
        raw.append(commit)
    elif commit:
        raw.extend(commit)
    refs: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in refs:
            refs.append(text)
    return refs


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


__all__ = [
    "CACHEABLE",
    "DEFAULT_LEASE_SECONDS",
    "EXECUTOR_VERSION",
    "TERMINAL",
    "Executor",
]
