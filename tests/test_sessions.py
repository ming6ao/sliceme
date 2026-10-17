"""Suspend/resume: the descriptor, the resume plan, and the registry.

These pin the session contract from ``docs/sessions.md``:

* the adapter-written descriptor is read by the engine and projected into
  ``campaign_sessions``;
* resume reconciles from git plus ``state.db`` (which win), never from a
  status-only rule that would re-run a verified-but-undelivered node;
* a preserved campaign worktree maps an interrupted node to ``paused`` and
  requests a phase record on resume;
* the pause control flag can be written and cleared;
* the ``checks`` cache table is additive on an older plane.
"""

import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign
from sliceme.service import Service
from sliceme.store import Store
from sliceme.util import db_path, write_json

FAKE_GH_BIN = Path(__file__).resolve().parent / "bin"


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class SessionsCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    nodes = [
        {"id": "w1", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]},
        {"id": "w2", "owns": ["src/b"], "depends_on": [], "acceptance": ["true"]},
        {"id": "w3", "owns": ["src/c"], "depends_on": ["w1"], "acceptance": ["true"]},
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        for sub in ("a", "b", "c"):
            (self.root / "src" / sub).mkdir(parents=True)
        (self.root / "src" / "a" / "x.py").write_text("a = 1\n")
        (self.root / "src" / "b" / "y.py").write_text("b = 1\n")
        (self.root / "src" / "c" / "z.py").write_text("c = 1\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        self.remote_tmp = tempfile.TemporaryDirectory()
        remote = Path(self.remote_tmp.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        run("git", "remote", "add", "origin", str(remote), cwd=self.root)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(FAKE_GH_BIN) + os.pathsep + self._old_path
        Service.init_plane(self.root, feature_branch="feat/x", checks=self.checks)
        self.svc = Service(self.root)
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "sessions",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": 3,
                "nodes": self.nodes,
            },
        )

    def tearDown(self):
        if self.svc is not None:
            self.svc.close()
        os.environ["PATH"] = self._old_path
        self.remote_tmp.cleanup()
        self.tmp.cleanup()

    # ---- helpers ------------------------------------------------------
    def edit(self, worktree, rel, content):
        (Path(worktree) / rel).write_text(content)

    def write_state(self, nodes, *, current_phase=0, branch="feat/x"):
        """Write the descriptor node map (the one place a node status lives)."""
        campaign.write_session(
            self.root,
            branch,
            self.descriptor(branch, nodes=nodes, current_phase=current_phase),
        )

    def descriptor(self, branch="feat/x", nodes=None, current_phase=0, **over):
        data = {
            "campaign": "sessions",
            "feature_branch": branch,
            "worktree_branch": self.svc.config.get("worktree_branch"),
            "design": "DESIGN.md",
            "pi": {
                "session_id": "sess-1",
                "session_file": "/tmp/sess-1.jsonl",
                "cwd": str(self.root),
            },
            "label": "sessions",
            "status": "suspended",
            "reason": "user",
            "suspended_at": 1733234400.0,
            "current_phase": current_phase,
            "nodes": nodes or {},
        }
        data.update(over)
        return data

    def record_w1(self):
        unit = self.svc.create_campaign_workspace()
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")
        result = self.svc.record_phase(0, messages={"w1": "test"})
        return next(c for c in result["records"] if c["node"] == "w1")

    def approve(self):
        return self.svc.record_approval(action="approve", actor="test")


class DescriptorTests(SessionsCase):
    def test_descriptor_round_trip_and_listing(self):
        campaign.write_session(self.root, "feat/x", self.descriptor())
        loaded = campaign.load_session(self.root, "feat/x")
        self.assertEqual(loaded["pi"]["session_id"], "sess-1")
        entries = campaign.list_sessions(self.root)
        self.assertEqual([key for key, _ in entries], ["feat--x"])

    def test_control_flag_can_be_written_and_cleared(self):
        campaign.write_control(
            self.root, "feat/x", {"pause": True, "requested_at": 1.0, "label": "x"}
        )
        self.assertTrue(campaign.load_control(self.root, "feat/x")["pause"])
        campaign.write_control(self.root, "feat/x", None)
        self.assertIsNone(campaign.load_control(self.root, "feat/x"))


class ResumePlanTests(SessionsCase):
    def test_delivered_record_is_done_and_never_re_run(self):
        record = self.record_w1()
        self.svc.store.update_record(int(record["id"]), status="delivered")
        self.svc.store.conn.commit()
        self.write_state({"w1": {"status": "done", "commit": record["head_commit"]}})

        plan = self.svc.resume()
        self.assertEqual(plan["nodes"]["w1"], "recorded")
        self.assertEqual(plan["resume_plan"]["record_phase"], None)
        self.assertNotIn("w1", plan["resume_plan"]["respawn"])
        self.assertNotIn("w1", plan["resume_plan"]["verify"])

    def test_verified_but_undelivered_node_stays_done(self):
        # The record is still `recorded` because nothing was delivered, yet the
        # node is recorded and its recorded commit matches: a status-only rule
        # would wrongly re-run it.
        record = self.record_w1()
        self.write_state({"w1": {"status": "done", "commit": record["head_commit"]}})

        plan = self.svc.resume()
        self.assertEqual(plan["nodes"]["w1"], "recorded")
        self.assertNotIn("w1", plan["resume_plan"]["respawn"])

    def test_record_with_recorded_commit_is_verified(self):
        record = self.record_w1()
        self.write_state({"w1": {"status": "recorded", "commit": record["head_commit"]}})

        plan = self.svc.resume()
        self.assertEqual(plan["nodes"]["w1"], "recorded")
        self.assertIn("w1", plan["resume_plan"]["verify"])

    def test_record_reads_its_commit_from_sqlite(self):
        # Neither the state entry nor the descriptor carries a commit, so the
        # record's own head_commit is the recorded commit and the node is
        # verified instead of re-spawned.
        self.record_w1()
        self.write_state({"w1": {"status": "running", "attempts": 1}})

        plan = self.svc.resume()
        self.assertEqual(plan["nodes"]["w1"], "recorded")
        self.assertIn("w1", plan["resume_plan"]["verify"])
        self.assertNotIn("w1", plan["resume_plan"]["respawn"])

    def test_resume_reads_progress_from_sqlite_without_state(self):
        # No state file is written.  The recorded record alone marks w1
        # recorded, and the cursor advances past phase 0 because its other
        # member (w2) recorded an empty marker.
        self.record_w1()
        campaign.write_session(self.root, "feat/x", self.descriptor(current_phase=None))

        plan = self.svc.resume()
        self.assertEqual(plan["nodes"]["w1"], "recorded")
        self.assertIn("w1", plan["resume_plan"]["verify"])
        self.assertEqual(plan["current_phase"], 1)

    def test_an_empty_marker_resumes_as_recorded(self):
        # w1 changes nothing, so its only record is an empty marker with no
        # commit.  A descriptor commit from an earlier attempt must not make the
        # empty marker borrow a sibling's head to look recorded.
        self.svc.create_campaign_workspace()
        recorded = self.svc.record_phase(
            0, messages={"w1": "no-op", "w2": "no-op"}
        )
        self.assertEqual(sorted(recorded["empty"]), ["w1", "w2"])
        self.write_state({"w1": {"status": "recorded", "commit": "0" * 40}})

        plan = self.svc.resume()
        self.assertEqual(plan["nodes"]["w1"], "recorded")
        self.assertIn("w1", plan["resume_plan"]["verify"])
        self.assertNotIn("w1", plan["resume_plan"]["respawn"])

    def test_preserved_worktree_maps_to_running_and_records_the_phase(self):
        unit = self.svc.create_campaign_workspace()
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")  # uncommitted
        self.write_state({"w1": {"status": "running", "attempts": 1}})

        plan = self.svc.resume()
        self.assertEqual(plan["nodes"]["w1"], "running")
        self.assertEqual(plan["resume_plan"]["record_phase"], 0)
        self.assertIn("w1", plan["resume_plan"]["resume"])

    def test_missing_worktree_falls_back_to_a_fresh_spawn(self):
        unit = self.svc.create_campaign_workspace()
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")
        shutil.rmtree(unit["worktree"])
        self.write_state({"w1": {"status": "running", "attempts": 1}})

        plan = self.svc.resume()
        self.assertFalse(plan["worktree_present"])
        self.assertEqual(plan["nodes"]["w1"], "pending")
        self.assertIn("w1", plan["resume_plan"]["respawn"])

    def test_fresh_pending_nodes_are_not_reported_as_respawn(self):
        self.write_state({"w1": {"status": "pending"}})
        plan = self.svc.resume()
        self.assertEqual(plan["nodes"]["w1"], "pending")
        self.assertNotIn("w1", plan["resume_plan"]["respawn"])

    def test_resume_is_idempotent(self):
        record = self.record_w1()
        self.write_state({"w1": {"status": "recorded", "commit": record["head_commit"]}})
        self.assertEqual(self.svc.resume(), self.svc.resume())

    def test_record_on_resume_then_verify(self):
        # Simulate a suspend while w1's worker was mid-edit: the worktree is
        # dirty and the node is running. Resume must ask for a record, and after
        # recording the node becomes a recorded record to verify.
        unit = self.svc.create_campaign_workspace()
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")
        self.write_state({"w1": {"status": "running", "attempts": 1}})

        before = self.svc.resume()
        self.assertEqual(before["resume_plan"]["record_phase"], 0)
        self.assertIn("w1", before["resume_plan"]["resume"])

        recorded = self.svc.record_phase(0, messages={"w1": "test"})
        record = next(c for c in recorded["records"] if c["node"] == "w1")
        self.write_state({"w1": {"status": "recorded", "commit": record["head_commit"]}})

        after = self.svc.resume()
        self.assertEqual(after["nodes"]["w1"], "recorded")
        self.assertIn("w1", after["resume_plan"]["verify"])
        self.assertIsNone(after["resume_plan"]["record_phase"])


class RegistryTests(SessionsCase):
    def test_sessions_lists_descriptors_and_projects_them(self):
        # Progress is counted from the recorded records in SQLite; the
        # descriptor carries only the adapter's session binding and node map.
        self.record_w1()
        campaign.write_session(self.root, "feat/x", self.descriptor())

        result = self.svc.sessions()
        entry = next(s for s in result["sessions"] if s["work_branch"] == "feat/x")
        self.assertTrue(entry["is_current"])
        self.assertEqual(entry["session_file"], "/tmp/sess-1.jsonl")
        self.assertEqual(entry["total"], 3)
        self.assertEqual(entry["done"], 1)

    def test_resume_is_a_pure_plan(self):
        campaign.write_session(self.root, "feat/x", self.descriptor())
        plan = self.svc.resume()
        self.assertEqual(plan["work_branch"], "feat/x")
        # The descriptor file remains the only registry; resume writes no rows.
        self.assertEqual(campaign.load_session(self.root, "feat/x")["pi"]["session_id"], "sess-1")


class DeliveryCleanupTests(SessionsCase):
    def branch_exists(self, branch):
        return (
            subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                cwd=self.root,
                capture_output=True,
                text=True,
            ).returncode
            == 0
        )

    def test_approved_delivery_removes_the_campaign_worktree_and_branch(self):
        self.record_w1()
        row = self.svc.store.get_campaign("feat/x")
        worktree = Path(row["worktree"])
        branch = row["work_branch"]
        # The campaign worktree and its branch exist before delivery.
        self.assertTrue(worktree.exists())
        self.assertTrue(self.branch_exists(branch))

        self.approve()
        delivered = self.svc.deliver(cleanup="worktrees")

        self.assertEqual([r["status"] for r in delivered["results"]], ["delivered"])
        # The approved work is opened as a pull request; the target branch is
        # untouched and the disposable branch is dropped by the cleanup.
        self.assertTrue(delivered["pull_request"]["url"])
        self.assertEqual((self.root / "src" / "a" / "x.py").read_text(), "a = 1\n")
        # ...and the worktree and its disposable branch are gone.
        self.assertFalse(worktree.exists())
        self.assertFalse(self.branch_exists(branch))

    def test_default_delivery_keeps_the_campaign_worktree(self):
        self.record_w1()
        row = self.svc.store.get_campaign("feat/x")
        self.approve()
        self.svc.deliver()
        # Cleanup is opt-in: the default keeps the worktree for inspection.
        self.assertTrue(Path(row["worktree"]).exists())

    def test_full_cleanup_removes_every_artifact_but_the_report(self):
        self.record_w1()
        campaign.write_session(self.root, "feat/x", self.descriptor())
        campaign.write_control(self.root, "feat/x", {"pause": False})
        campaign.worker_log_path(self.root, "feat/x", "w1").write_text("log\n")
        self.svc.report(narrative="done")
        self.approve()

        delivered = self.svc.deliver(cleanup="all")

        self.assertEqual([r["status"] for r in delivered["results"]], ["delivered"])
        for path in (
            campaign.dag_path(self.root, "feat/x"),
            campaign.session_path(self.root, "feat/x"),
            campaign.control_path(self.root, "feat/x"),
            campaign.worker_log_path(self.root, "feat/x", "w1"),
        ):
            self.assertFalse(path.exists(), str(path))
        # The report is review evidence and survives every cleanup level.
        self.assertTrue(campaign.report_path(self.root, "feat/x").exists())


class MigrationTests(unittest.TestCase):
    def test_additive_tables_appear_on_an_older_plane(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run("git", "init", "-q", "-b", "main", cwd=root)
            run("git", "config", "user.email", "t@example.com", cwd=root)
            run("git", "config", "user.name", "Tester", cwd=root)
            (root / "a.txt").write_text("hi\n")
            run("git", "add", "-A", cwd=root)
            run("git", "commit", "-qm", "init", cwd=root)
            (root / ".sliceme").mkdir()
            db = db_path(root)
            conn = sqlite3.connect(str(db))
            conn.execute(
                "CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " name TEXT UNIQUE NOT NULL, task TEXT,"
                " attachment TEXT NOT NULL DEFAULT 'terminal', created_at REAL NOT NULL)"
            )
            conn.commit()
            conn.close()

            store = Store(root)
            tables = {
                row[0]
                for row in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("verdicts", tables)
            self.assertIn("approvals", tables)
            self.assertNotIn("jobs", tables)
            columns = {
                row[1] for row in store.conn.execute("PRAGMA table_info(verdicts)")
            }
            self.assertIn("fingerprint", columns)
            self.assertIn("results", columns)
            self.assertIn("status", columns)
            store.close()

    def test_a_legacy_checks_table_is_copied(self):
        # An old ``checks`` table is renamed to ``verdicts`` and its rows are
        # copied.  The retired phase number is not carried over.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".sliceme").mkdir()
            conn = sqlite3.connect(str(db_path(root)))
            conn.execute(
                "CREATE TABLE checks ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " fingerprint TEXT NOT NULL,"
                " campaign TEXT,"
                " source TEXT NOT NULL,"
                " commit_ref TEXT NOT NULL,"
                " tree TEXT,"
                " commands TEXT NOT NULL,"
                " checks TEXT,"
                " sandbox TEXT,"
                " sandbox_digest TEXT,"
                " status TEXT NOT NULL,"
                " duration REAL,"
                " exit_code INTEGER,"
                " output TEXT,"
                " results TEXT,"
                " error TEXT,"
                " created_at REAL NOT NULL,"
                " finished_at REAL)"
            )
            conn.execute(
                "INSERT INTO checks(fingerprint, source, commit_ref,"
                " commands, status, created_at)"
                " VALUES('f', 'phase:3', 'c', '[]', 'passed', 0)"
            )
            conn.commit()
            conn.close()

            store = Store(root)
            tables = {
                row[0]
                for row in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("checks", tables)
            row = store.conn.execute("SELECT source FROM verdicts").fetchone()
            self.assertEqual(row[0], "phase:3")
            store.close()

    def test_retired_tables_are_dropped_on_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".sliceme").mkdir()
            conn = sqlite3.connect(str(db_path(root)))
            for table in ("jobs", "attempts", "comments"):
                conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()

            store = Store(root)
            tables = {
                row[0]
                for row in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("jobs", tables)
            self.assertNotIn("attempts", tables)
            self.assertNotIn("comments", tables)
            self.assertIn("verdicts", tables)
            store.close()


if __name__ == "__main__":
    unittest.main()
