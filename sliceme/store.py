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
        self._ensure_columns("candidates", {"node": "TEXT"})
        self._ensure_columns("candidates", {"empty": "INTEGER NOT NULL DEFAULT 0"})
        self._ensure_columns("candidates", {"campaign": "TEXT"})
        self._ensure_columns("campaigns", {"worktree": "TEXT"})
        self._rename_branch_columns()
        self._backfill_base_branch()
        self._drop_unit_schema()
        self._rename_verdict_phase_column()
        self._rename_state_tables()

    def _rename_verdict_phase_column(self) -> None:
        """Rename the retired ``verdicts.wave`` column to ``phase``.

        The wave term is retired, so a plane created before the rename keeps
        its verdict rows and gains the new column name.
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
            self.conn.execute(
                "INSERT OR IGNORE INTO verdicts(id, fingerprint, phase, campaign,"
                " source, commit_ref, tree, commands, checks, sandbox, sandbox_digest,"
                " status, duration, exit_code, output, results, error,"
                " created_at, finished_at)"
                " SELECT id, fingerprint, wave, campaign, source, commit_ref, tree,"
                " commands, checks, sandbox, sandbox_digest, status, duration,"
                " exit_code, output, results, error, created_at, finished_at"
                " FROM checks"
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
    ) -> dict[str, Any]:
        """Register a campaign.  Idempotent on the unique key or work branch."""
        ts = now()
        unit_name = unit_name or f"campaign:{key}"
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO campaigns(key, work_branch, worktree,"
                " base_branch, unit_name, name, design, state, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    work_branch,
                    worktree,
                    base_branch,
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
    ) -> int:
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO records(campaign, head_commit, status, summary,"
                " node, empty, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (campaign, head_commit, "recorded", summary, node, int(empty), ts, ts),
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

    def latest_approvals_by_commit(self, branch_key: str) -> dict[str, dict[str, Any]]:
        """The newest approval for each reviewed commit (ascending id: last wins)."""
        rows = self.conn.execute(
            "SELECT * FROM approvals WHERE branch_key=?"
            " AND commit_hash IS NOT NULL ORDER BY id ASC",
            (branch_key,),
        ).fetchall()
        found: dict[str, dict[str, Any]] = {}
        for row in rows:
            found[str(row["commit_hash"])] = _dict(row)  # type: ignore[assignment]
        return found

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
