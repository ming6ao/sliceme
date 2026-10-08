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
  pr_url TEXT,
  pr_number INTEGER,
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

CREATE TABLE IF NOT EXISTS checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL,
  wave INTEGER,
  campaign TEXT,
  source TEXT NOT NULL,
  commit_ref TEXT NOT NULL,
  tree TEXT,
  commands TEXT NOT NULL,
  checks TEXT,
  sandbox TEXT,
  sandbox_digest TEXT,
  gpu TEXT NOT NULL DEFAULT 'none',
  status TEXT NOT NULL,
  duration REAL,
  exit_code INTEGER,
  output TEXT,
  results TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  finished_at REAL
);

CREATE INDEX IF NOT EXISTS idx_checks_fingerprint ON checks(fingerprint);
CREATE INDEX IF NOT EXISTS idx_checks_campaign ON checks(campaign);
CREATE INDEX IF NOT EXISTS idx_checks_commit ON checks(commit_ref, id);

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

-- Retired tables from the executor queue.  They are dropped on every open so
-- an upgraded plane loses the dead schema (the synchronous check runner and the
-- campaign lock replace them).
DROP TABLE IF EXISTS jobs;
DROP TABLE IF EXISTS attempts;
DROP TABLE IF EXISTS comments;
"""

#: Check verdicts a later run may reuse from the cache.  A ``cancelled`` or
#: ``running`` row does not exist: the runner writes one terminal row per run.
CHECK_VERDICTS = ("passed", "failed", "error")


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
        self._ensure_columns("units", {"campaign": "TEXT"})
        self._ensure_columns("candidates", {"campaign": "TEXT"})
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_candidates_campaign ON candidates(campaign)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_units_campaign ON units(campaign)"
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

    def set_campaign_pull_request(
        self, ref: str | int, *, url: str, number: int | None = None
    ) -> dict[str, Any] | None:
        """Record the delivery pull request on a campaign row."""
        self.conn.execute(
            "UPDATE campaigns SET pr_url=?, pr_number=?, updated_at=?"
            " WHERE key=? OR target_branch=? OR unit_name=? OR id=?",
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

    def latest_check_for_commit(self, commit_ref: str) -> dict[str, Any] | None:
        """The newest terminal check for a commit (the review evidence)."""
        placeholders = ",".join("?" for _ in CHECK_VERDICTS)
        return _dict(
            self.conn.execute(
                f"SELECT * FROM checks WHERE commit_ref=? AND status IN ({placeholders})"
                " ORDER BY id DESC LIMIT 1",
                (commit_ref, *CHECK_VERDICTS),
            ).fetchone()
        )

    def require_unit(self, name_or_id: str | int) -> dict[str, Any]:
        unit = self.get_unit(name_or_id)
        if unit is None:
            raise SlicemeError(f"unknown unit: {name_or_id}")
        return unit

    # ---- checks (the persistent cache) --------------------------------
    def create_check(
        self,
        *,
        fingerprint: str,
        source: str,
        commit_ref: str,
        status: str,
        commands: list[str] | None = None,
        checks: list[dict[str, Any]] | None = None,
        wave: int | None = None,
        campaign: str | None = None,
        tree: str | None = None,
        sandbox: dict[str, Any] | None = None,
        sandbox_digest: str | None = None,
        gpu: str = "none",
        duration: float | None = None,
        exit_code: int | None = None,
        output: str | None = None,
        results: str | None = None,
        error: str | None = None,
    ) -> int:
        """Write one terminal check row and return its id."""
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO checks(fingerprint, wave, campaign, source, commit_ref,"
                " tree, commands, checks, sandbox, sandbox_digest, gpu, status, duration,"
                " exit_code, output, results, error, created_at, finished_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fingerprint,
                    wave,
                    campaign,
                    source,
                    commit_ref,
                    tree,
                    json.dumps(commands if commands is not None else []),
                    json.dumps(checks) if checks is not None else None,
                    json.dumps(sandbox) if sandbox is not None else None,
                    sandbox_digest,
                    gpu,
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

    def get_check(self, check_id: str | int) -> dict[str, Any] | None:
        return _dict(
            self.conn.execute(
                "SELECT * FROM checks WHERE id=?", (int(check_id),)
            ).fetchone()
        )

    def find_check(
        self, fingerprint: str, *, statuses: Sequence[str] | None = None
    ) -> dict[str, Any] | None:
        """The newest check row for *fingerprint*, newest first."""
        sql = "SELECT * FROM checks WHERE fingerprint=?"
        params: list[Any] = [fingerprint]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY id DESC LIMIT 1"
        return _dict(self.conn.execute(sql, params).fetchone())

    def list_checks(
        self,
        *,
        statuses: Sequence[str] | None = None,
        campaign: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM checks"
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

    def check_counts(self, *, campaign: str | None = None) -> dict[str, int]:
        if campaign is None:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS c FROM checks GROUP BY status"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS c FROM checks WHERE campaign=? GROUP BY status",
                (campaign,),
            ).fetchall()
        return {str(r["status"]): int(r["c"]) for r in rows}

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

    def prune_reviews(
        self, *, keep_branch_keys: set[str], keep_after: float
    ) -> dict[str, int]:
        """Delete review decisions older than *keep_after* for unkept branches."""
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
        return {"decisions": int(decisions)}
