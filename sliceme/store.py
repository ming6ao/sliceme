"""SQLite store for the local plane (WAL).

The local store is SQLite/WAL (``docs/reference.md`` §3).  The
service layer owns all business rules; this module owns persistence.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .util import SlicemeError, branch_key, db_path, now

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,
  target_branch TEXT NOT NULL UNIQUE,
  worktree_branch TEXT NOT NULL UNIQUE,
  base TEXT,
  unit_name TEXT NOT NULL UNIQUE,
  name TEXT,
  design TEXT,
  state TEXT NOT NULL DEFAULT 'working',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_campaigns_state ON campaigns(state);

CREATE TABLE IF NOT EXISTS units (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  campaign TEXT,
  kind TEXT NOT NULL DEFAULT 'worker',
  worktree TEXT NOT NULL,
  branch TEXT NOT NULL,
  base_commit TEXT,
  state TEXT NOT NULL DEFAULT 'working',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  unit_id INTEGER NOT NULL REFERENCES units(id),
  campaign TEXT,
  head_commit TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'prepared',
  summary TEXT,
  node TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_candidates_campaign ON candidates(campaign);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  wave INTEGER,
  campaign TEXT,
  requester TEXT,
  source TEXT NOT NULL,
  commit_ref TEXT NOT NULL,
  tree TEXT,
  commands TEXT NOT NULL,
  sandbox TEXT,
  sandbox_digest TEXT,
  gpu TEXT NOT NULL DEFAULT 'none',
  priority INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued',
  fingerprint TEXT,
  timeout INTEGER NOT NULL DEFAULT 3600,
  requested_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL,
  duration REAL,
  exit_code INTEGER,
  output TEXT,
  error TEXT,
  runner_pid INTEGER
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint, status);
CREATE INDEX IF NOT EXISTS idx_jobs_campaign ON jobs(campaign);

CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node TEXT NOT NULL,
  unit TEXT,
  campaign TEXT,
  attempt INTEGER NOT NULL DEFAULT 1,
  agent TEXT NOT NULL DEFAULT 'worker',
  status TEXT NOT NULL DEFAULT 'running',
  started_at REAL NOT NULL,
  finished_at REAL,
  duration REAL,
  exit_code INTEGER,
  turns INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  tools TEXT,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  last_tool TEXT,
  last_activity_at REAL,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_attempts_node ON attempts(node);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON attempts(status);

CREATE TABLE IF NOT EXISTS review_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  branch_key TEXT NOT NULL,
  commit_hash TEXT,
  action TEXT NOT NULL,
  actor TEXT,
  note TEXT,
  created_at REAL NOT NULL,
  consumed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_review_decisions_commit
  ON review_decisions(branch_key, commit_hash, id);

CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  branch_key TEXT NOT NULL,
  commit_hash TEXT,
  file TEXT,
  side TEXT,
  line INTEGER,
  line_end INTEGER,
  body TEXT NOT NULL,
  node TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  created_at REAL NOT NULL,
  addressed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_comments_branch
  ON comments(branch_key, status, id);
"""


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _dicts(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{k: r[k] for k in r.keys()} for r in rows]


class Store:
    def __init__(self, root: Path, *, migrate: bool = True):
        self.root = root
        path = db_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        if migrate:
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self) -> None:
        """Additive column migrations for planes created by older versions."""
        self._ensure_columns("jobs", {"timeout": "INTEGER NOT NULL DEFAULT 3600"})
        self._ensure_columns("candidates", {"node": "TEXT"})
        self._ensure_columns("units", {"campaign": "TEXT"})
        self._ensure_columns("candidates", {"campaign": "TEXT"})
        self._ensure_columns("jobs", {"campaign": "TEXT"})
        self._ensure_columns("attempts", {"campaign": "TEXT"})
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_candidates_campaign ON candidates(campaign)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_units_campaign ON units(campaign)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempts_campaign ON attempts(campaign)"
        )

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {
            row["name"]
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, decl in columns.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- campaigns ----------------------------------------------------
    def create_campaign(
        self,
        *,
        key: str,
        target_branch: str,
        worktree_branch: str,
        base: str | None = None,
        unit_name: str | None = None,
        name: str | None = None,
        design: str | None = None,
        state: str = "working",
    ) -> dict[str, Any]:
        """Register a campaign.  Idempotent on the unique key or target."""
        ts = now()
        unit_name = unit_name or f"campaign:{key}"
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO campaigns(key, target_branch, worktree_branch,"
                " base, unit_name, name, design, state, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    target_branch,
                    worktree_branch,
                    base,
                    unit_name,
                    name,
                    design,
                    state,
                    ts,
                    ts,
                ),
            )
            row = c.execute(
                "SELECT * FROM campaigns WHERE key=?", (key,)
            ).fetchone()
        return _dict(row)  # type: ignore[return-value]

    def get_campaign(self, ref: str | int | None) -> dict[str, Any] | None:
        """Find a campaign by key, target branch, unit name, or id."""
        if ref is None:
            return None
        text = str(ref)
        if text.isdigit():
            row = self.conn.execute(
                "SELECT * FROM campaigns WHERE id=?", (int(text),)
            ).fetchone()
            if row is not None:
                return _dict(row)
        row = self.conn.execute(
            "SELECT * FROM campaigns WHERE key=? OR target_branch=? OR unit_name=?"
            " OR name=? ORDER BY id LIMIT 1",
            (text, text, text, text),
        ).fetchone()
        return _dict(row)

    def require_campaign(self, ref: str | int | None) -> dict[str, Any]:
        campaign = self.get_campaign(ref)
        if campaign is None:
            raise SlicemeError(f"unknown campaign: {ref}")
        return campaign

    def list_campaigns(self, *, state: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM campaigns"
        params: list[Any] = []
        if state:
            sql += " WHERE state=?"
            params.append(state)
        sql += " ORDER BY id"
        return _dicts(self.conn.execute(sql, params).fetchall())

    def set_campaign_state(self, ref: str | int, state: str) -> dict[str, Any] | None:
        self.conn.execute(
            "UPDATE campaigns SET state=?, updated_at=? WHERE key=? OR target_branch=?"
            " OR unit_name=? OR id=?",
            (state, now(), str(ref), str(ref), str(ref), int(ref) if str(ref).isdigit() else -1),
        )
        return self.get_campaign(ref)

    def update_campaign_target(
        self, key: str, *, target_branch: str, worktree_branch: str | None = None
    ) -> dict[str, Any] | None:
        sets = ["target_branch=?", "key=?", "updated_at=?"]
        params: list[Any] = [target_branch, branch_key(target_branch), now()]
        if worktree_branch is not None:
            sets.insert(1, "worktree_branch=?")
            params.insert(1, worktree_branch)
        params.append(key)
        self.conn.execute(f"UPDATE campaigns SET {', '.join(sets)} WHERE key=?", params)
        return self.get_campaign(branch_key(target_branch))

    # ---- units --------------------------------------------------------
    def create_unit(
        self,
        *,
        name: str,
        kind: str,
        worktree: str,
        branch: str,
        base_commit: str,
        campaign: str | None = None,
    ) -> int:
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO units(name, campaign, kind, worktree, branch, base_commit,"
                " state, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (name, campaign, kind, worktree, branch, base_commit, "working", ts, ts),
            )
            row = c.execute("SELECT id FROM units WHERE name=?", (name,)).fetchone()
        return int(row["id"])

    def set_unit_state(self, unit_id: int, state: str) -> None:
        self.conn.execute(
            "UPDATE units SET state=?, updated_at=? WHERE id=?", (state, now(), unit_id)
        )

    def get_unit(self, name_or_id: str | int) -> dict[str, Any] | None:
        if isinstance(name_or_id, int) or str(name_or_id).isdigit():
            row = self.conn.execute(
                "SELECT * FROM units WHERE id=?", (int(name_or_id),)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM units WHERE name=? ORDER BY id DESC LIMIT 1", (name_or_id,)
            ).fetchone()
        return _dict(row)

    def get_unit_by_campaign(self, campaign: str) -> dict[str, Any] | None:
        """The campaign's accumulation unit, if it exists."""
        row = self.conn.execute(
            "SELECT * FROM units WHERE campaign=? ORDER BY id DESC LIMIT 1", (campaign,)
        ).fetchone()
        return _dict(row)

    def list_units(
        self, *, active_only: bool = False, campaign: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if active_only:
            clauses.append("state='active'")
        if campaign is not None:
            clauses.append("campaign=?")
            params.append(campaign)
        sql = "SELECT * FROM units"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return _dicts(self.conn.execute(sql, params).fetchall())

    # ---- candidates ---------------------------------------------------
    def create_candidate(
        self,
        *,
        unit_id: int,
        head_commit: str,
        summary: str | None,
        node: str | None = None,
        campaign: str | None = None,
    ) -> int:
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO candidates(unit_id, campaign, head_commit, status, summary,"
                " node, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (unit_id, campaign, head_commit, "prepared", summary, node, ts, ts),
            )
            return int(c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def update_candidate(
        self,
        candidate_id: int,
        *,
        status: str | None = None,
        head_commit: str | None = None,
    ) -> None:
        sets = ["updated_at=?"]
        params: list[Any] = [now()]
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if head_commit is not None:
            sets.append("head_commit=?")
            params.append(head_commit)
        params.append(candidate_id)
        self.conn.execute(f"UPDATE candidates SET {', '.join(sets)} WHERE id=?", params)

    def get_candidate(self, name_or_id: str | int) -> dict[str, Any] | None:
        select = (
            "SELECT c.*, u.name AS unit_name, u.branch AS unit_branch,"
            " u.worktree AS worktree, u.base_commit AS unit_base_commit"
            " FROM candidates c JOIN units u ON u.id=c.unit_id"
        )
        text = str(name_or_id)
        if text.isdigit():
            row = self.conn.execute(select + " WHERE c.id=?", (int(text),)).fetchone()
        else:
            row = self.conn.execute(
                select + " WHERE u.name=? OR c.node=? ORDER BY c.id DESC LIMIT 1",
                (text, text),
            ).fetchone()
        return _dict(row)

    def list_candidates(
        self,
        *,
        statuses: Sequence[str] | None = None,
        campaign: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT c.*, u.name AS unit_name, u.worktree AS worktree,"
            " u.branch AS unit_branch, u.base_commit AS unit_base_commit"
            " FROM candidates c JOIN units u ON u.id=c.unit_id"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"c.status IN ({placeholders})")
            params.extend(statuses)
        if campaign is not None:
            clauses.append("c.campaign=?")
            params.append(campaign)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY c.created_at ASC, c.id ASC"
        return _dicts(self.conn.execute(sql, params).fetchall())

    def latest_job_for_commit(self, commit_ref: str) -> dict[str, Any] | None:
        """The newest terminal check job for a commit (the review evidence)."""
        return _dict(
            self.conn.execute(
                "SELECT * FROM jobs WHERE commit_ref=?"
                " AND status IN ('passed', 'failed', 'error') ORDER BY id DESC LIMIT 1",
                (commit_ref,),
            ).fetchone()
        )

    def require_unit(self, name_or_id: str | int) -> dict[str, Any]:
        unit = self.get_unit(name_or_id)
        if unit is None:
            raise SlicemeError(f"unknown unit: {name_or_id}")
        return unit

    # ---- executor jobs ------------------------------------------------
    JOB_FIELDS = frozenset(
        {
            "wave",
            "campaign",
            "requester",
            "source",
            "commit_ref",
            "tree",
            "commands",
            "sandbox",
            "sandbox_digest",
            "gpu",
            "priority",
            "status",
            "fingerprint",
            "timeout",
            "requested_at",
            "started_at",
            "finished_at",
            "duration",
            "exit_code",
            "output",
            "error",
            "runner_pid",
        }
    )

    def create_job(
        self,
        *,
        source: str,
        commit_ref: str,
        commands: list[str],
        wave: int | None = None,
        campaign: str | None = None,
        requester: str | None = None,
        tree: str | None = None,
        sandbox: dict[str, Any] | None = None,
        sandbox_digest: str | None = None,
        gpu: str = "none",
        priority: int = 0,
        fingerprint: str | None = None,
        timeout: int = 3600,
    ) -> int:
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO jobs(wave, campaign, requester, source, commit_ref, tree,"
                " commands, sandbox, sandbox_digest, gpu, priority, status, fingerprint,"
                " timeout, requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    wave,
                    campaign,
                    requester,
                    source,
                    commit_ref,
                    tree,
                    json.dumps(commands),
                    json.dumps(sandbox) if sandbox is not None else None,
                    sandbox_digest,
                    gpu,
                    priority,
                    "queued",
                    fingerprint,
                    int(timeout),
                    ts,
                ),
            )
            return int(c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def get_job(self, job_id: str | int) -> dict[str, Any] | None:
        return _dict(
            self.conn.execute("SELECT * FROM jobs WHERE id=?", (int(job_id),)).fetchone()
        )

    def list_jobs(
        self, *, statuses: Sequence[str] | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs"
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY priority DESC, requested_at ASC, id ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return _dicts(self.conn.execute(sql, params).fetchall())

    def update_job(self, job_id: str | int, **fields: Any) -> None:
        unknown = set(fields) - self.JOB_FIELDS
        if unknown:
            raise SlicemeError(f"unknown job fields: {', '.join(sorted(unknown))}")
        if not fields:
            return
        sets = ", ".join(f"{name}=?" for name in fields)
        params = list(fields.values()) + [int(job_id)]
        self.conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", params)

    def claim_next_job(self, *, runner_pid: int | None = None) -> dict[str, Any] | None:
        """Atomically claim the highest-priority queued job."""
        with self.tx() as c:
            row = c.execute(
                "SELECT * FROM jobs WHERE status='queued'"
                " ORDER BY priority DESC, requested_at ASC, id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE jobs SET status='running', started_at=?, runner_pid=? WHERE id=?",
                (now(), runner_pid, int(row["id"])),
            )
            claimed = c.execute("SELECT * FROM jobs WHERE id=?", (int(row["id"]),)).fetchone()
            return _dict(claimed)

    def find_passed_job(self, fingerprint: str) -> dict[str, Any] | None:
        """A passing job for the same fingerprint, for cache/dedupe."""
        return _dict(
            self.conn.execute(
                "SELECT * FROM jobs WHERE fingerprint=? AND status='passed'"
                " ORDER BY id DESC LIMIT 1",
                (fingerprint,),
            ).fetchone()
        )

    def recover_orphan_jobs(self, *, cutoff: float) -> int:
        """Reset ``running`` jobs older than *cutoff* back to ``queued``."""
        with self.tx() as c:
            cursor = c.execute(
                "UPDATE jobs SET status='queued', started_at=NULL, runner_pid=NULL,"
                " error='recovered orphaned lease'"
                " WHERE status='running' AND (started_at IS NULL OR started_at < ?)",
                (cutoff,),
            )
            return int(cursor.rowcount)

    def job_counts(self, *, campaign: str | None = None) -> dict[str, int]:
        if campaign is None:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS c FROM jobs GROUP BY status"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS c FROM jobs WHERE campaign=? GROUP BY status",
                (campaign,),
            ).fetchall()
        return {str(r["status"]): int(r["c"]) for r in rows}

    # ---- attempts (per-subagent fidelity) ----------------------------
    ATTEMPT_FIELDS = frozenset(
        {
            "node",
            "unit",
            "campaign",
            "attempt",
            "agent",
            "status",
            "started_at",
            "finished_at",
            "duration",
            "exit_code",
            "turns",
            "tool_calls",
            "tools",
            "tokens_in",
            "tokens_out",
            "cost",
            "last_tool",
            "last_activity_at",
            "error",
        }
    )

    def create_attempt(
        self,
        *,
        node: str,
        unit: str | None = None,
        campaign: str | None = None,
        attempt: int = 1,
        agent: str = "worker",
        started_at: float | None = None,
    ) -> int:
        ts = now() if started_at is None else float(started_at)
        with self.tx() as c:
            c.execute(
                "INSERT INTO attempts(node, unit, campaign, attempt, agent, status,"
                " started_at, last_activity_at) VALUES(?,?,?,?,?,?,?,?)",
                (node, unit, campaign, int(attempt), agent, "running", ts, ts),
            )
            return int(c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def finish_attempt(self, attempt_id: int, **fields: Any) -> dict[str, Any] | None:
        unknown = set(fields) - self.ATTEMPT_FIELDS
        if unknown:
            raise SlicemeError(f"unknown attempt fields: {', '.join(sorted(unknown))}")
        ts = now()
        row = _dict(
            self.conn.execute("SELECT * FROM attempts WHERE id=?", (int(attempt_id),)).fetchone()
        )
        if row is None:
            return None
        fields.setdefault("status", "ok")
        fields.setdefault("finished_at", ts)
        started = float(row.get("started_at") or ts)
        fields.setdefault("duration", ts - started)
        sets = ", ".join(f"{name}=?" for name in fields)
        params = list(fields.values()) + [int(attempt_id)]
        self.conn.execute(f"UPDATE attempts SET {sets} WHERE id=?", params)
        return _dict(
            self.conn.execute("SELECT * FROM attempts WHERE id=?", (int(attempt_id),)).fetchone()
        )

    def get_attempt(self, attempt_id: str | int) -> dict[str, Any] | None:
        return _dict(
            self.conn.execute("SELECT * FROM attempts WHERE id=?", (int(attempt_id),)).fetchone()
        )

    def list_attempts(
        self,
        *,
        node: str | None = None,
        statuses: Sequence[str] | None = None,
        campaign: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM attempts"
        clauses: list[str] = []
        params: list[Any] = []
        if node:
            clauses.append("node=?")
            params.append(node)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if campaign is not None:
            clauses.append("campaign=?")
            params.append(campaign)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id ASC"
        return _dicts(self.conn.execute(sql, params).fetchall())

    def latest_attempt(
        self, node: str, *, campaign: str | None = None
    ) -> dict[str, Any] | None:
        sql = "SELECT * FROM attempts WHERE node=?"
        params: list[Any] = [node]
        if campaign is not None:
            sql += " AND campaign=?"
            params.append(campaign)
        sql += " ORDER BY id DESC LIMIT 1"
        return _dict(self.conn.execute(sql, params).fetchone())

    def find_running_attempt(
        self, node: str, attempt: int | None = None, *, campaign: str | None = None
    ) -> dict[str, Any] | None:
        sql = "SELECT * FROM attempts WHERE node=? AND status='running'"
        params: list[Any] = [node]
        if attempt is not None:
            sql += " AND attempt=?"
            params.append(int(attempt))
        if campaign is not None:
            sql += " AND campaign=?"
            params.append(campaign)
        sql += " ORDER BY id DESC LIMIT 1"
        return _dict(self.conn.execute(sql, params).fetchone())

    # ---- review decisions (per-commit approvals) ----------------------
    def add_review_decision(
        self,
        *,
        branch_key: str,
        action: str,
        commit_hash: str | None = None,
        actor: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Append one decision.  The newest row for a commit wins."""
        with self.tx() as c:
            c.execute(
                "INSERT INTO review_decisions(branch_key, commit_hash, action, actor,"
                " note, created_at) VALUES(?,?,?,?,?,?)",
                (branch_key, commit_hash, action, actor, note, now()),
            )
            return _dict(
                c.execute(
                    "SELECT * FROM review_decisions WHERE id=last_insert_rowid()"
                ).fetchone()
            )  # type: ignore[return-value]

    def latest_review_decision(
        self, branch_key: str, commit_hash: str | None = None
    ) -> dict[str, Any] | None:
        if commit_hash is None:
            sql = (
                "SELECT * FROM review_decisions WHERE branch_key=?"
                " AND commit_hash IS NULL ORDER BY id DESC LIMIT 1"
            )
            params: tuple[Any, ...] = (branch_key,)
        else:
            sql = (
                "SELECT * FROM review_decisions WHERE branch_key=? AND commit_hash=?"
                " ORDER BY id DESC LIMIT 1"
            )
            params = (branch_key, commit_hash)
        return _dict(self.conn.execute(sql, params).fetchone())

    def list_review_decisions(
        self, *, branch_key: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM review_decisions"
        params: list[Any] = []
        if branch_key:
            sql += " WHERE branch_key=?"
            params.append(branch_key)
        sql += " ORDER BY id ASC"
        return _dicts(self.conn.execute(sql, params).fetchall())

    def latest_decisions_by_commit(self, branch_key: str) -> dict[str, dict[str, Any]]:
        """The newest decision for each reviewed commit (ascending id: last wins)."""
        rows = self.conn.execute(
            "SELECT * FROM review_decisions WHERE branch_key=?"
            " AND commit_hash IS NOT NULL ORDER BY id ASC",
            (branch_key,),
        ).fetchall()
        found: dict[str, dict[str, Any]] = {}
        for row in rows:
            found[str(row["commit_hash"])] = _dict(row)  # type: ignore[assignment]
        return found

    def consume_review_decisions(self, decision_ids: Sequence[int]) -> None:
        ids = [int(item) for item in decision_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(
            f"UPDATE review_decisions SET consumed_at=? WHERE id IN ({placeholders})"
            " AND consumed_at IS NULL",
            [now(), *ids],
        )

    # ---- comments -----------------------------------------------------
    def add_comment(
        self,
        *,
        branch_key: str,
        body: str,
        commit_hash: str | None = None,
        file: str | None = None,
        side: str | None = None,
        line: int | None = None,
        line_end: int | None = None,
        node: str | None = None,
    ) -> dict[str, Any]:
        with self.tx() as c:
            c.execute(
                "INSERT INTO comments(branch_key, commit_hash, file, side, line,"
                " line_end, body, node, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    branch_key,
                    commit_hash,
                    file,
                    side,
                    line,
                    line_end if line_end is not None else line,
                    body,
                    node,
                    "open",
                    now(),
                ),
            )
            return _dict(
                c.execute("SELECT * FROM comments WHERE id=last_insert_rowid()").fetchone()
            )  # type: ignore[return-value]

    def get_comment(self, comment_id: str | int) -> dict[str, Any] | None:
        return _dict(
            self.conn.execute(
                "SELECT * FROM comments WHERE id=?", (int(comment_id),)
            ).fetchone()
        )

    def list_comments(
        self,
        *,
        branch_key: str | None = None,
        statuses: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM comments"
        clauses: list[str] = []
        params: list[Any] = []
        if branch_key:
            clauses.append("branch_key=?")
            params.append(branch_key)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id ASC"
        return _dicts(self.conn.execute(sql, params).fetchall())

    def set_comment_status(
        self, comment_id: int, status: str, *, addressed_at: float | None = None
    ) -> dict[str, Any] | None:
        self.conn.execute(
            "UPDATE comments SET status=?, addressed_at=? WHERE id=?",
            (status, addressed_at, int(comment_id)),
        )
        return self.get_comment(comment_id)

    def prune_reviews(
        self, *, keep_branch_keys: set[str], keep_after: float
    ) -> dict[str, int]:
        """Delete review rows older than *keep_after* for unkept branches."""
        clause = ""
        params: list[Any] = [keep_after]
        if keep_branch_keys:
            keys = sorted(keep_branch_keys)
            clause = " AND branch_key NOT IN (" + ",".join("?" for _ in keys) + ")"
            params.extend(keys)
        with self.tx() as c:
            decisions = c.execute(
                "DELETE FROM review_decisions WHERE created_at < ?" + clause, params
            ).rowcount
            comments = c.execute(
                "DELETE FROM comments WHERE created_at < ?" + clause, params
            ).rowcount
        return {"decisions": int(decisions), "comments": int(comments)}
