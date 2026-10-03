"""Suspend/resume: the descriptor, the resume plan, the registry, and attempts.

These pin the session contract from ``docs/sessions.md``:

* the adapter-written descriptor is read by the engine and projected into
  ``campaign_sessions``;
* resume reconciles from git plus ``state.db`` (which win), never from a
  status-only rule that would re-run a verified-but-undelivered node;
* a preserved campaign worktree maps an interrupted node to ``paused`` and
  requests a wave record on resume;
* the pause control flag can be written and cleared;
* the ``attempts`` table is additive on an older plane.
"""

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


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class SessionsCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    nodes = [
        {"id": "w1", "owns": ["dir:src/a"], "depends_on": [], "acceptance": ["true"]},
        {"id": "w2", "owns": ["dir:src/b"], "depends_on": [], "acceptance": ["true"]},
        {"id": "w3", "owns": ["dir:src/c"], "depends_on": ["w1"], "acceptance": ["true"]},
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
        run("git", "checkout", "-q", "-b", "feat/x", cwd=self.root)
        Service.init_plane(self.root, checks=self.checks)
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
        self.tmp.cleanup()

    # ---- helpers ------------------------------------------------------
    def edit(self, worktree, rel, content):
        (Path(worktree) / rel).write_text(content)

    def write_state(self, nodes, *, waves=None, current_wave=0, branch="feat/x"):
        write_json(
            campaign.state_path(self.root, branch),
            {
                "campaign": "sessions",
                "feature_branch": branch,
                "waves": waves
                if waves is not None
                else [
                    {"index": 0, "members": ["w1", "w2"], "status": "pending", "integrated": []}
                ],
                "current_wave": current_wave,
                "nodes": nodes,
            },
        )

    def descriptor(self, branch="feat/x", **over):
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
            "current_wave": 0,
        }
        data.update(over)
        return data

    def record_w1(self):
        unit = self.svc.create_campaign_workspace(base="feat/x")
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")
        result = self.svc.record_wave(0)
        return next(c for c in result["candidates"] if c["node"] == "w1")

    def approve(self):
        return self.svc.review_decision(action="approve", all_commits=True, actor="test")


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
    def test_landed_candidate_is_done_and_never_re_run(self):
        candidate = self.record_w1()
        self.svc.store.update_candidate(int(candidate["id"]), status="landed")
        self.svc.store.conn.commit()
        self.write_state({"w1": {"status": "done", "commit": candidate["head_commit"]}})
        campaign.write_session(self.root, "feat/x", self.descriptor())

        plan = self.svc.resume(plan_only=True)
        self.assertEqual(plan["nodes"]["w1"], "done")
        self.assertEqual(plan["resume_plan"]["record_wave"], None)
        self.assertNotIn("w1", plan["resume_plan"]["respawn"])
        self.assertNotIn("w1", plan["resume_plan"]["verify"])

    def test_verified_but_undelivered_node_stays_done(self):
        # The candidate is still `prepared` because nothing was delivered, yet the
        # node is done and its recorded commit matches: a status-only rule would
        # wrongly re-run it.
        candidate = self.record_w1()
        self.write_state({"w1": {"status": "done", "commit": candidate["head_commit"]}})
        campaign.write_session(self.root, "feat/x", self.descriptor())

        plan = self.svc.resume(plan_only=True)
        self.assertEqual(plan["nodes"]["w1"], "done")
        self.assertNotIn("w1", plan["resume_plan"]["respawn"])

    def test_prepared_candidate_with_recorded_commit_is_verified(self):
        candidate = self.record_w1()
        self.write_state({"w1": {"status": "recorded", "commit": candidate["head_commit"]}})
        campaign.write_session(self.root, "feat/x", self.descriptor())

        plan = self.svc.resume(plan_only=True)
        self.assertEqual(plan["nodes"]["w1"], "recorded")
        self.assertIn("w1", plan["resume_plan"]["verify"])

    def test_prepared_candidate_without_recorded_commit_is_respawned(self):
        self.record_w1()
        self.write_state({"w1": {"status": "running", "attempts": 1}})
        campaign.write_session(self.root, "feat/x", self.descriptor())

        plan = self.svc.resume(plan_only=True)
        self.assertEqual(plan["nodes"]["w1"], "pending")
        self.assertIn("w1", plan["resume_plan"]["respawn"])

    def test_preserved_worktree_maps_to_paused_and_records_the_wave(self):
        unit = self.svc.create_campaign_workspace(base="feat/x")
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")  # uncommitted
        self.write_state({"w1": {"status": "running", "attempts": 1}})
        campaign.write_session(self.root, "feat/x", self.descriptor())

        plan = self.svc.resume(plan_only=True)
        self.assertEqual(plan["nodes"]["w1"], "paused")
        self.assertEqual(plan["resume_plan"]["record_wave"], 0)
        self.assertIn("w1", plan["resume_plan"]["resume"])

    def test_missing_worktree_falls_back_to_a_fresh_spawn(self):
        unit = self.svc.create_campaign_workspace(base="feat/x")
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")
        shutil.rmtree(unit["worktree"])
        self.write_state({"w1": {"status": "running", "attempts": 1}})
        campaign.write_session(self.root, "feat/x", self.descriptor())

        plan = self.svc.resume(plan_only=True)
        self.assertFalse(plan["worktree_present"])
        self.assertEqual(plan["nodes"]["w1"], "pending")
        self.assertIn("w1", plan["resume_plan"]["respawn"])

    def test_fresh_pending_nodes_are_not_reported_as_respawn(self):
        self.write_state({"w1": {"status": "pending"}})
        campaign.write_session(self.root, "feat/x", self.descriptor())
        plan = self.svc.resume(plan_only=True)
        self.assertEqual(plan["nodes"]["w1"], "pending")
        self.assertNotIn("w1", plan["resume_plan"]["respawn"])

    def test_resume_is_idempotent(self):
        candidate = self.record_w1()
        self.write_state({"w1": {"status": "recorded", "commit": candidate["head_commit"]}})
        campaign.write_session(self.root, "feat/x", self.descriptor())
        self.assertEqual(self.svc.resume(plan_only=True), self.svc.resume(plan_only=True))

    def test_record_on_resume_then_verify(self):
        # Simulate a suspend while w1's worker was mid-edit: the worktree is
        # dirty and the node is running. Resume must ask for a record, and after
        # recording the node becomes a prepared candidate to verify.
        unit = self.svc.create_campaign_workspace(base="feat/x")
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")
        self.write_state({"w1": {"status": "running", "attempts": 1}})
        campaign.write_session(self.root, "feat/x", self.descriptor())

        before = self.svc.resume(plan_only=True)
        self.assertEqual(before["resume_plan"]["record_wave"], 0)
        self.assertIn("w1", before["resume_plan"]["resume"])

        recorded = self.svc.record_wave(0)
        candidate = next(c for c in recorded["candidates"] if c["node"] == "w1")
        self.write_state({"w1": {"status": "recorded", "commit": candidate["head_commit"]}})

        after = self.svc.resume(plan_only=True)
        self.assertEqual(after["nodes"]["w1"], "recorded")
        self.assertIn("w1", after["resume_plan"]["verify"])
        self.assertIsNone(after["resume_plan"]["record_wave"])


class RegistryTests(SessionsCase):
    def test_sessions_lists_descriptors_and_projects_them(self):
        self.write_state({"w1": {"status": "done"}, "w2": {"status": "pending"}})
        campaign.write_session(self.root, "feat/x", self.descriptor())

        result = self.svc.sessions()
        entry = next(s for s in result["sessions"] if s["feature_branch"] == "feat/x")
        self.assertTrue(entry["is_current"])
        self.assertEqual(entry["session_file"], "/tmp/sess-1.jsonl")
        self.assertEqual(entry["total"], 3)
        self.assertEqual(entry["done"], 1)

    def test_resume_is_a_pure_plan(self):
        campaign.write_session(self.root, "feat/x", self.descriptor())
        plan = self.svc.resume(plan_only=True)
        self.assertEqual(plan["feature_branch"], "feat/x")
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
        unit = self.svc.store.get_unit("campaign")
        worktree = Path(unit["worktree"])
        branch = unit["branch"]
        # The campaign worktree and its branch exist before delivery.
        self.assertTrue(worktree.exists())
        self.assertTrue(self.branch_exists(branch))

        self.approve()
        delivered = self.svc.deliver(cleanup="worktrees")

        self.assertEqual([r["status"] for r in delivered["results"]], ["landed"])
        # The approved merge landed on the target...
        self.assertEqual((self.root / "src" / "a" / "x.py").read_text(), "a = 2\n")
        # ...and the worktree and its disposable branch are gone.
        self.assertFalse(worktree.exists())
        self.assertFalse(self.branch_exists(branch))

    def test_default_delivery_keeps_the_campaign_worktree(self):
        self.record_w1()
        unit = self.svc.store.get_unit("campaign")
        self.approve()
        self.svc.deliver()
        # Cleanup is opt-in: the default keeps the worktree for inspection.
        self.assertTrue(Path(unit["worktree"]).exists())


class AttemptTests(SessionsCase):
    def test_attempt_begin_and_end_record_metrics(self):
        started = self.svc.begin_attempt(node="w1", unit="campaign", attempt=1)
        self.assertEqual(started["status"], "running")
        finished = self.svc.end_attempt(
            node="w1",
            attempt=1,
            status="ok",
            exit_code=0,
            turns=7,
            tool_calls=23,
            tokens_in=45210,
            tokens_out=3120,
            cost=0.42,
            last_tool="bash",
        )
        self.assertEqual(finished["status"], "ok")
        self.assertEqual(finished["turns"], 7)
        self.assertEqual(finished["tool_calls"], 23)
        self.assertIsNotNone(finished["duration"])
        self.assertEqual(self.svc.attempts(node="w1")["attempts"][0]["node"], "w1")

    def test_end_without_a_running_attempt_is_rejected(self):
        self.assertIsNone(self.svc.end_attempt(node="ghost", attempt=1))


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
            self.assertIn("attempts", tables)
            self.assertIn("review_decisions", tables)
            self.assertIn("comments", tables)
            store.close()


if __name__ == "__main__":
    unittest.main()
