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

from .util import SlicemeError, db_path, now

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,
  work_branch TEXT NOT NULL UNIQUE,
  worktree TEXT,
  base_branch TEXT,
  unit_name TEXT NOT NULL UNIQUE,
  name TEXT,
  design TEXT,
  state TEXT NOT NULL DEFAULT 'working',
  delivery_mode TEXT,
  merge_commit TEXT,
  pr_url TEXT,
  pr_number INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_campaigns_state ON campaigns(state);

CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign TEXT,
  head_commit TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'recorded',
  summary TEXT,
  node TEXT,
  empty INTEGER NOT NULL DEFAULT 0,
  tries INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_status ON records(status);
CREATE INDEX IF NOT EXISTS idx_records_campaign ON records(campaign);

CREATE TABLE IF NOT EXISTS verdicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL,
  phase INTEGER,
  campaign TEXT,
  source TEXT NOT NULL,
  commit_ref TEXT NOT NULL,
  tree TEXT,
  commands TEXT NOT NULL,
  checks TEXT,
  sandbox TEXT,
  sandbox_digest TEXT,
  status TEXT NOT NULL,
  duration REAL,
  exit_code INTEGER,
  output TEXT,
  results TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  finished_at REAL
);

CREATE INDEX IF NOT EXISTS idx_verdicts_fingerprint ON verdicts(fingerprint);
CREATE INDEX IF NOT EXISTS idx_verdicts_campaign ON verdicts(campaign);
CREATE INDEX IF NOT EXISTS idx_verdicts_commit ON verdicts(commit_ref, id);

CREATE TABLE IF NOT EXISTS timings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign TEXT,
  step_id TEXT NOT NULL,
  run TEXT,
  agent TEXT,
  child_index INTEGER,
  kind TEXT NOT NULL,
  tool TEXT,
  command TEXT,
  command_truncated INTEGER NOT NULL DEFAULT 0,
  is_error INTEGER NOT NULL DEFAULT 0,
  duration REAL NOT NULL,
  started_at REAL,
  ended_at REAL,
  source TEXT,
  created_at REAL NOT NULL,
  UNIQUE (campaign, step_id)
);

CREATE INDEX IF NOT EXISTS idx_timings_campaign ON timings(campaign);
CREATE INDEX IF NOT EXISTS idx_timings_kind ON timings(campaign, kind, duration);

CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  branch_key TEXT NOT NULL,
  commit_hash TEXT,
  action TEXT NOT NULL,
  actor TEXT,
  note TEXT,
  created_at REAL NOT NULL,
  consumed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_approvals_commit
  ON approvals(branch_key, commit_hash, id);

-- Retired tables from the executor queue.  They are dropped on every open so
-- an upgraded plane loses the dead schema (the synchronous check runner and the
-- campaign lock replace them).
DROP TABLE IF EXISTS jobs;
DROP TABLE IF EXISTS attempts;
DROP TABLE IF EXISTS comments;
"""

#: The canonical ``campaigns`` DDL, reused by the branch-column migration.
_CAMPAIGNS_TABLE = """
CREATE TABLE campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,
  work_branch TEXT NOT NULL UNIQUE,
  worktree TEXT,
  base_branch TEXT,
  unit_name TEXT NOT NULL UNIQUE,
  name TEXT,
  design TEXT,
  state TEXT NOT NULL DEFAULT 'working',
  delivery_mode TEXT,
  merge_commit TEXT,
  pr_url TEXT,
  pr_number INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
)
"""

#: Verdict statuses a later run may reuse from the cache.  A ``cancelled`` or
#: ``running`` row does not exist: the runner writes one terminal row per run.
VERDICT_STATUSES = ("passed", "failed", "error")


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
        self._ensure_columns(
            "campaigns", {"pr_url": "TEXT", "pr_number": "INTEGER"}
        )
        self._ensure_columns(
            "campaigns", {"delivery_mode": "TEXT", "merge_commit": "TEXT"}
        )
        # The per-node try count that `record` derives from the progress
        # channel: a node with a salvage retake records `tries = 2`.
        self._ensure_columns("records", {"tries": "INTEGER NOT NULL DEFAULT 1"})
        self._ensure_columns("candidates", {"node": "TEXT"})
        self._ensure_columns("candidates", {"empty": "INTEGER NOT NULL DEFAULT 0"})
        self._ensure_columns("candidates", {"campaign": "TEXT"})
        self._ensure_columns("campaigns", {"worktree": "TEXT"})
        self._rename_branch_columns()
        self._backfill_base_branch()
        self._drop_unit_schema()
        self._rename_verdict_phase_column()
        self._ensure_columns("verdicts", {"phase": "INTEGER"})
        self._rename_state_tables()

    def _rename_verdict_phase_column(self) -> None:
        """Rename the retired ``verdicts.wave`` column to ``phase``.

        The wave term is retired.  A plane created before the rename keeps its
        verdict rows and gains the new column name.  A plane with neither
        column gets a nullable ``phase`` from the follow-up
        :meth:`_ensure_columns` call.  The retired ``gpu`` column stays: it is
        harmless, and a column drop needs a table rebuild on older SQLite.
        """
        if "verdicts" not in {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }:
            return
        existing = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(verdicts)").fetchall()
        }
        if "wave" in existing and "phase" not in existing:
            self.conn.execute("ALTER TABLE verdicts RENAME COLUMN wave TO phase")

    def _rename_state_tables(self) -> None:
        """Rename the retired state tables and their statuses.

        ``candidates`` becomes ``records`` with the statuses ``recorded`` and
        ``delivered``; ``checks`` becomes ``verdicts``; ``review_decisions``
        becomes ``approvals``.  A fresh plane already has the new tables, so
        this is a no-op there.
        """
        tables = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "candidates" in tables:
            self.conn.execute(
                "INSERT OR IGNORE INTO records(id, campaign, head_commit, status,"
                " summary, node, empty, created_at, updated_at)"
                " SELECT id, campaign, head_commit,"
                " CASE status WHEN 'prepared' THEN 'recorded'"
                " WHEN 'landed' THEN 'delivered' ELSE status END,"
                " summary, node, empty, created_at, updated_at FROM candidates"
            )
            self.conn.execute("DROP TABLE candidates")
        if "checks" in tables:
            # A very old plane named the phase column ``wave``.  Copy the
            # phase value when the source has one; a plane with neither
            # column copies no phase and keeps a null.
            checks_columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(checks)").fetchall()
            }
            source_phase = (
                "wave" if "wave" in checks_columns else "phase" if "phase" in checks_columns else None
            )
            target_columns = (
                "id, fingerprint, campaign, source, commit_ref, tree, commands,"
                " checks, sandbox, sandbox_digest, status, duration, exit_code,"
                " output, results, error, created_at, finished_at"
            )
            source_columns = target_columns
            if source_phase:
                target_columns = target_columns.replace(
                    "fingerprint, campaign", f"fingerprint, phase, campaign", 1
                )
                source_columns = source_columns.replace(
                    "fingerprint, campaign", f"fingerprint, {source_phase}, campaign", 1
                )
            self.conn.execute(
                f"INSERT OR IGNORE INTO verdicts({target_columns})"
                f" SELECT {source_columns} FROM checks"
            )
            self.conn.execute("DROP TABLE checks")
        if "review_decisions" in tables:
            self.conn.execute(
                "INSERT OR IGNORE INTO approvals(id, branch_key, commit_hash, action,"
                " actor, note, created_at, consumed_at)"
                " SELECT id, branch_key, commit_hash, action, actor, note, created_at,"
                " consumed_at FROM review_decisions"
            )
            self.conn.execute("DROP TABLE review_decisions")

    def _drop_unit_schema(self) -> None:
        """Retire the ``units`` table and the ``candidates.unit_id`` column.

        The campaign owns the one worktree, so older planes keep their unit
        data only long enough to backfill ``campaigns.worktree`` and rebuild
        ``candidates`` without the dropped foreign key.  A fresh plane already
        has neither, so this is a no-op there.  The rebuild runs before
        ``DROP TABLE units``: SQLite ignores ``PRAGMA foreign_keys`` inside a
        transaction (the ``worktree`` backfill opens one), so a legacy
        ``candidates`` row that still references ``units`` would fail the drop
        with a foreign-key violation.
        """
        tables = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "units" in tables:
            self._backfill_campaign_worktree()
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(candidates)").fetchall()
        }
        if "unit_id" in columns:
            self.conn.execute("ALTER TABLE candidates RENAME TO candidates_legacy")
            self.conn.execute(
                "CREATE TABLE candidates ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, campaign TEXT,"
                " head_commit TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'recorded',"
                " summary TEXT, node TEXT, empty INTEGER NOT NULL DEFAULT 0,"
                " created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            self.conn.execute(
                "INSERT INTO candidates(id, campaign, head_commit, status, summary,"
                " node, empty, created_at, updated_at)"
                " SELECT id, campaign, head_commit, status, summary, node, empty,"
                " created_at, updated_at FROM candidates_legacy"
            )
            self.conn.execute("DROP TABLE candidates_legacy")
        if "units" in tables:
            self.conn.execute("DROP TABLE units")

    def _rename_branch_columns(self) -> None:
        """Collapse the campaign branch names onto ``work_branch``/``base_branch``.

        Older planes kept four names for one branch (``target_branch`` and
        ``worktree_branch``) and two for the pull request base
        (``delivery_base`` and ``base``).  The campaign owns one work branch
        and one base branch, so the migration rebuilds the table with the
        canonical columns and copies the values once.  A fresh plane already
        has them, so this is a no-op there.
        """
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(campaigns)").fetchall()
        }
        if "work_branch" in columns or "target_branch" not in columns:
            return
        work_expr = (
            "COALESCE(worktree_branch, target_branch)"
            if "worktree_branch" in columns
            else "target_branch"
        )
        if "delivery_base" in columns and "base" in columns:
            base_expr = "COALESCE(delivery_base, base)"
        elif "delivery_base" in columns:
            base_expr = "delivery_base"
        elif "base" in columns:
            base_expr = "base"
        else:
            base_expr = "NULL"
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("ALTER TABLE campaigns RENAME TO campaigns_legacy")
        self.conn.execute(_CAMPAIGNS_TABLE)
        self.conn.execute(
            "INSERT INTO campaigns(id, key, work_branch, worktree, base_branch,"
            " unit_name, name, design, state, pr_url, pr_number, created_at, updated_at)"
            f" SELECT id, key, {work_expr}, worktree, {base_expr}, unit_name, name,"
            " design, state, pr_url, pr_number, created_at, updated_at"
            " FROM campaigns_legacy"
        )
        self.conn.execute("DROP TABLE campaigns_legacy")
        self.conn.execute("PRAGMA foreign_keys=ON")

    def _backfill_base_branch(self) -> None:
        """Fill a missing base branch from the repository default branch.

        The base branch is the pull request base.  Older rows have no value,
        so the migration records the found default branch once.
        """
        missing = self.conn.execute(
            "SELECT COUNT(*) AS c FROM campaigns WHERE base_branch IS NULL"
        ).fetchone()["c"]
        if not missing:
            return
        from .integrate import found_default_branch

        self.conn.execute(
            "UPDATE campaigns SET base_branch=? WHERE base_branch IS NULL",
            (found_default_branch(self.root),),
        )

    def _backfill_campaign_worktree(self) -> None:
        """Fill a missing campaign worktree from its campaign unit.

        The campaign owns one worktree.  Older rows kept it only on the
        ``units`` row, so the migration copies it once so every reader can use
        ``campaigns.worktree``.
        """
        missing = self.conn.execute(
            "SELECT COUNT(*) AS c FROM campaigns WHERE worktree IS NULL"
        ).fetchone()["c"]
        if not missing:
            return
        self.conn.execute(
            "UPDATE campaigns SET worktree = ("
            " SELECT u.worktree FROM units u"
            " WHERE u.campaign = campaigns.key AND u.kind='campaign'"
            " ORDER BY u.id DESC LIMIT 1"
            ") WHERE worktree IS NULL"
        )

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        if table not in {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }:
            return
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
        work_branch: str,
        worktree: str | None = None,
        base_branch: str | None = None,
        unit_name: str | None = None,
        name: str | None = None,
        design: str | None = None,
        state: str = "working",
        delivery_mode: str | None = None,
        merge_commit: str | None = None,
    ) -> dict[str, Any]:
        """Register a campaign.  Idempotent on the unique key or work branch."""
        ts = now()
        unit_name = unit_name or f"campaign:{key}"
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO campaigns(key, work_branch, worktree,"
                " base_branch, unit_name, name, design, state, delivery_mode,"
                " merge_commit, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    work_branch,
                    worktree,
                    base_branch,
                    unit_name,
                    name,
                    design,
                    state,
                    delivery_mode,
                    merge_commit,
                    ts,
                    ts,
                ),
            )
            row = c.execute(
                "SELECT * FROM campaigns WHERE key=?", (key,)
            ).fetchone()
        return _dict(row)  # type: ignore[return-value]

    def get_campaign(self, ref: str | int | None) -> dict[str, Any] | None:
        """Find a campaign by key, work branch, unit name, or id."""
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
            "SELECT * FROM campaigns WHERE key=? OR work_branch=? OR unit_name=?"
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
            "UPDATE campaigns SET state=?, updated_at=? WHERE key=? OR work_branch=?"
            " OR unit_name=? OR id=?",
            (state, now(), str(ref), str(ref), str(ref), int(ref) if str(ref).isdigit() else -1),
        )
        return self.get_campaign(ref)

    def set_campaign_pull_request(
        self, ref: str | int, *, url: str, number: int | None = None
    ) -> dict[str, Any] | None:
        """Record the delivery pull request on a campaign row."""
        self.conn.execute(
            "UPDATE campaigns SET pr_url=?, pr_number=?, updated_at=?"
            " WHERE key=? OR work_branch=? OR unit_name=? OR id=?",
            (
                url,
                number,
                now(),
                str(ref),
                str(ref),
                str(ref),
                int(ref) if str(ref).isdigit() else -1,
            ),
        )
        self.conn.commit()
        return self.get_campaign(ref)

    def set_campaign_delivery_mode(
        self, ref: str | int, mode: str
    ) -> dict[str, Any] | None:
        """Record the delivery mode on a campaign row."""
        self.conn.execute(
            "UPDATE campaigns SET delivery_mode=?, updated_at=?"
            " WHERE key=? OR work_branch=? OR unit_name=? OR id=?",
            (
                mode,
                now(),
                str(ref),
                str(ref),
                str(ref),
                int(ref) if str(ref).isdigit() else -1,
            ),
        )
        self.conn.commit()
        return self.get_campaign(ref)

    def set_campaign_merge_commit(
        self, ref: str | int, commit: str
    ) -> dict[str, Any] | None:
        """Record the local merge commit on a campaign row."""
        self.conn.execute(
            "UPDATE campaigns SET merge_commit=?, updated_at=?"
            " WHERE key=? OR work_branch=? OR unit_name=? OR id=?",
            (
                commit,
                now(),
                str(ref),
                str(ref),
                str(ref),
                int(ref) if str(ref).isdigit() else -1,
            ),
        )
        self.conn.commit()
        return self.get_campaign(ref)

    def set_campaign_worktree(self, key: str, worktree: str) -> None:
        """Record the campaign's one worktree path."""
        self.conn.execute(
            "UPDATE campaigns SET worktree=?, updated_at=? WHERE key=?",
            (worktree, now(), key),
        )
        self.conn.commit()

    # ---- records ------------------------------------------------------
    def create_record(
        self,
        *,
        head_commit: str,
        summary: str | None,
        node: str | None = None,
        campaign: str | None = None,
        empty: bool = False,
        tries: int = 1,
    ) -> int:
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO records(campaign, head_commit, status, summary,"
                " node, empty, tries, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    campaign,
                    head_commit,
                    "recorded",
                    summary,
                    node,
                    int(empty),
                    max(1, int(tries)),
                    ts,
                    ts,
                ),
            )
            return int(c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def update_record(
        self,
        record_id: int,
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
        params.append(record_id)
        self.conn.execute(f"UPDATE records SET {', '.join(sets)} WHERE id=?", params)

    def delete_records(self, record_ids: Sequence[int]) -> int:
        """Delete record rows by id and return the number removed.

        ``record --reopen`` uses this to drop one phase and every later phase.
        The caller resets the worktree first, so a dropped row never points at
        a missing commit.
        """
        ids = [int(item) for item in record_ids]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cursor = self.conn.execute(
            f"DELETE FROM records WHERE id IN ({placeholders})", ids
        )
        self.conn.commit()
        return int(cursor.rowcount)

    def get_record(self, name_or_id: str | int) -> dict[str, Any] | None:
        text = str(name_or_id)
        if text.isdigit():
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=?", (int(text),)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM records WHERE node=? ORDER BY id DESC LIMIT 1",
                (text,),
            ).fetchone()
        return _dict(row)

    def list_records(
        self,
        *,
        statuses: Sequence[str] | None = None,
        campaign: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM records"
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if campaign is not None:
            clauses.append("campaign=?")
            params.append(campaign)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC, id ASC"
        return _dicts(self.conn.execute(sql, params).fetchall())

    def latest_verdict_for_commit(self, commit_ref: str) -> dict[str, Any] | None:
        """The newest terminal verdict for a commit (the review evidence)."""
        placeholders = ",".join("?" for _ in VERDICT_STATUSES)
        return _dict(
            self.conn.execute(
                f"SELECT * FROM verdicts WHERE commit_ref=? AND status IN ({placeholders})"
                " ORDER BY id DESC LIMIT 1",
                (commit_ref, *VERDICT_STATUSES),
            ).fetchone()
        )

    # ---- verdicts (the persistent cache) ------------------------------
    def create_verdict(
        self,
        *,
        fingerprint: str,
        source: str,
        commit_ref: str,
        status: str,
        commands: list[str] | None = None,
        checks: list[dict[str, Any]] | None = None,
        phase: int | None = None,
        campaign: str | None = None,
        tree: str | None = None,
        sandbox: dict[str, Any] | None = None,
        sandbox_digest: str | None = None,
        duration: float | None = None,
        exit_code: int | None = None,
        output: str | None = None,
        results: str | None = None,
        error: str | None = None,
    ) -> int:
        """Write one terminal verdict row and return its id."""
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO verdicts(fingerprint, phase, campaign, source, commit_ref,"
                " tree, commands, checks, sandbox, sandbox_digest, status, duration,"
                " exit_code, output, results, error, created_at, finished_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fingerprint,
                    phase,
                    campaign,
                    source,
                    commit_ref,
                    tree,
                    json.dumps(commands if commands is not None else []),
                    json.dumps(checks) if checks is not None else None,
                    json.dumps(sandbox) if sandbox is not None else None,
                    sandbox_digest,
                    status,
                    duration,
                    exit_code,
                    output,
                    results,
                    error,
                    ts,
                    ts,
                ),
            )
            return int(c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def get_verdict(self, verdict_id: str | int) -> dict[str, Any] | None:
        return _dict(
            self.conn.execute(
                "SELECT * FROM verdicts WHERE id=?", (int(verdict_id),)
            ).fetchone()
        )

    def find_verdict(
        self, fingerprint: str, *, statuses: Sequence[str] | None = None
    ) -> dict[str, Any] | None:
        """The newest verdict row for *fingerprint*, newest first."""
        sql = "SELECT * FROM verdicts WHERE fingerprint=?"
        params: list[Any] = [fingerprint]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY id DESC LIMIT 1"
        return _dict(self.conn.execute(sql, params).fetchone())

    def list_verdicts(
        self,
        *,
        statuses: Sequence[str] | None = None,
        campaign: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM verdicts"
        params: list[Any] = []
        clauses: list[str] = []
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
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return _dicts(self.conn.execute(sql, params).fetchall())

    def verdict_counts(self, *, campaign: str | None = None) -> dict[str, int]:
        if campaign is None:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS c FROM verdicts GROUP BY status"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS c FROM verdicts WHERE campaign=? GROUP BY status",
                (campaign,),
            ).fetchall()
        return {str(r["status"]): int(r["c"]) for r in rows}

    # ---- timings (one row per thinking step or tool call) -------------
    def add_timings(self, rows: Sequence[dict[str, Any]]) -> int:
        """Insert timing rows, skipping steps that already exist.

        A step id is stable across a transcript re-read, so the ingest can run
        at every phase boundary without a duplicate row.
        """
        if not rows:
            return 0
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO timings(campaign, step_id, run, agent,"
            " child_index, kind, tool, command, command_truncated, is_error,"
            " duration, started_at, ended_at, source, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row.get("campaign"),
                    row.get("step_id"),
                    row.get("run"),
                    row.get("agent"),
                    int(row.get("child_index") or 0),
                    row.get("kind"),
                    row.get("tool"),
                    row.get("command"),
                    int(bool(row.get("command_truncated"))),
                    int(bool(row.get("is_error"))),
                    float(row.get("duration") or 0.0),
                    row.get("started_at"),
                    row.get("ended_at"),
                    row.get("source"),
                    float(row.get("created_at") or now()),
                )
                for row in rows
            ],
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def list_timings(self, *, campaign: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM timings"
        params: list[Any] = []
        if campaign is not None:
            sql += " WHERE campaign=?"
            params.append(campaign)
        sql += " ORDER BY started_at ASC, id ASC"
        return _dicts(self.conn.execute(sql, params).fetchall())

    def timing_runs(self, *, campaign: str) -> set[str]:
        """The run ids that already have timing rows for one campaign."""
        rows = self.conn.execute(
            "SELECT DISTINCT run FROM timings WHERE campaign=? AND run IS NOT NULL",
            (campaign,),
        ).fetchall()
        return {str(row["run"]) for row in rows}

    def delete_timings(self, *, campaign: str) -> int:
        cursor = self.conn.execute("DELETE FROM timings WHERE campaign=?", (campaign,))
        self.conn.commit()
        return int(cursor.rowcount)

    # ---- approvals (one campaign approval) ----------------------------
    def add_approval(
        self,
        *,
        branch_key: str,
        action: str,
        commit_hash: str | None = None,
        actor: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Append one approval.  The newest row for a commit wins."""
        with self.tx() as c:
            c.execute(
                "INSERT INTO approvals(branch_key, commit_hash, action, actor,"
                " note, created_at) VALUES(?,?,?,?,?,?)",
                (branch_key, commit_hash, action, actor, note, now()),
            )
            return _dict(
                c.execute(
                    "SELECT * FROM approvals WHERE id=last_insert_rowid()"
                ).fetchone()
            )  # type: ignore[return-value]

    def latest_approval(
        self, branch_key: str, commit_hash: str | None = None
    ) -> dict[str, Any] | None:
        if commit_hash is None:
            sql = (
                "SELECT * FROM approvals WHERE branch_key=?"
                " AND commit_hash IS NULL ORDER BY id DESC LIMIT 1"
            )
            params: tuple[Any, ...] = (branch_key,)
        else:
            sql = (
                "SELECT * FROM approvals WHERE branch_key=? AND commit_hash=?"
                " ORDER BY id DESC LIMIT 1"
            )
            params = (branch_key, commit_hash)
        return _dict(self.conn.execute(sql, params).fetchone())

    def list_approvals(
        self, *, branch_key: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM approvals"
        params: list[Any] = []
        if branch_key:
            sql += " WHERE branch_key=?"
            params.append(branch_key)
        sql += " ORDER BY id ASC"
        return _dicts(self.conn.execute(sql, params).fetchall())

    def void_approvals(self, branch_key: str) -> int:
        """Mark every live campaign approval for *branch_key* as void.

        A reopened phase changes the campaign history, so an earlier
        confirmation no longer covers it.  The row stays for audit evidence.
        """
        cursor = self.conn.execute(
            "UPDATE approvals SET action='void', consumed_at=?"
            " WHERE branch_key=? AND action='approve' AND consumed_at IS NULL",
            (now(), branch_key),
        )
        self.conn.commit()
        return int(cursor.rowcount)

    def consume_approvals(self, approval_ids: Sequence[int]) -> None:
        ids = [int(item) for item in approval_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(
            f"UPDATE approvals SET consumed_at=? WHERE id IN ({placeholders})"
            " AND consumed_at IS NULL",
            [now(), *ids],
        )

    def prune_approvals(
        self, *, keep_branch_keys: set[str], keep_after: float
    ) -> dict[str, int]:
        """Delete approvals older than *keep_after* for unkept branches."""
        clause = ""
        params: list[Any] = [keep_after]
        if keep_branch_keys:
            keys = sorted(keep_branch_keys)
            clause = " AND branch_key NOT IN (" + ",".join("?" for _ in keys) + ")"
            params.extend(keys)
        with self.tx() as c:
            approvals = c.execute(
                "DELETE FROM approvals WHERE created_at < ?" + clause, params
            ).rowcount
        return {"approvals": int(approvals)}
