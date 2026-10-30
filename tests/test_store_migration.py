"""Migrations for planes created by an older engine.

The `verdicts` table once named its phase column `wave`.  A later release
renamed the column, and a cleanup commit removed the migration.  These tests
pin the restore: an old plane keeps its verdict rows and gains `phase`.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sliceme.store import Store

OLD_VERDICTS = """
CREATE TABLE verdicts (
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
"""

OLD_CHECKS = """CREATE TABLE checks (
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
"""


NO_PHASE_VERDICTS = OLD_VERDICTS.replace("  wave INTEGER,\n", "")


class StoreMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".sliceme").mkdir()
        self.db = self.root / ".sliceme" / "state.db"

    def tearDown(self):
        self.tmp.cleanup()

    def sql(self, script: str, *rows: tuple) -> None:
        conn = sqlite3.connect(str(self.db))
        conn.executescript(script)
        for statement, values in rows:
            conn.execute(statement, values)
        conn.commit()
        conn.close()

    def test_wave_is_renamed_and_rows_are_kept(self):
        self.sql(
            OLD_VERDICTS,
            (
                "INSERT INTO verdicts(fingerprint, wave, source, commit_ref,"
                " commands, gpu, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("fp-1", 4, "phase:4", "abc", json.dumps(["true"]), "T1", "passed", 1.0),
            ),
        )
        with Store(self.root) as store:
            columns = {
                row["name"]
                for row in store.conn.execute("PRAGMA table_info(verdicts)").fetchall()
            }
            self.assertIn("phase", columns)
            self.assertNotIn("wave", columns)
            old = store.get_verdict(1)
            self.assertEqual(old["phase"], 4)
            self.assertEqual(old["status"], "passed")
            new_id = store.create_verdict(
                fingerprint="fp-2",
                source="phase:3",
                commit_ref="def",
                status="passed",
                phase=3,
            )
            self.assertEqual(store.get_verdict(new_id)["phase"], 3)

    def test_a_verdicts_table_with_neither_column_gains_phase(self):
        self.sql(
            NO_PHASE_VERDICTS,
        )
        with Store(self.root) as store:
            columns = {
                row["name"]
                for row in store.conn.execute("PRAGMA table_info(verdicts)").fetchall()
            }
            self.assertIn("phase", columns)
            store.create_verdict(
                fingerprint="fp-3",
                source="phase:0",
                commit_ref="ghi",
                status="passed",
                phase=0,
            )

    def test_a_legacy_checks_table_copies_its_phase(self):
        self.sql(
            OLD_CHECKS,
            (
                "INSERT INTO checks(id, fingerprint, wave, source, commit_ref,"
                " commands, gpu, status, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (7, "fp-4", 2, "phase:2", "jkl", json.dumps(["true"]), "none", "passed", 2.0),
            ),
        )
        with Store(self.root) as store:
            copied = store.get_verdict(7)
            self.assertEqual(copied["phase"], 2)
            tables = {
                row["name"]
                for row in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertNotIn("checks", tables)


if __name__ == "__main__":
    unittest.main()
