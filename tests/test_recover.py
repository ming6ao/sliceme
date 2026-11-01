"""The ``recover`` verb (``docs/progress-watchdog.md`` §5.1).

One verb serves the salvage evidence read and the single-node reset: it
prints the node's progress tail as JSON and runs
``git restore --source=HEAD -- <owned dir>``, so only that node's own
directory moves and the sibling work of the phase stays untouched.  The verb
refuses a finished campaign like ``reopen`` does and serializes with the
workspace lock like ``record``.  A node the session descriptor still marks
``running`` refuses the restore too: the workspace lock cannot see a live
pi child, so the reset would move the work under it.  A clean directory
skips the restore call; every entry, failed or not, carries the progress
row.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sliceme import campaign, surface
from sliceme.service import Service
from sliceme.util import SlicemeError, write_json


class RecoverCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src" / "a").mkdir(parents=True)
        (self.root / "src" / "b").mkdir(parents=True)
        (self.root / "src" / "a" / "x.py").write_text("x = 1\n")
        (self.root / "src" / "b" / "x.py").write_text("x = 1\n")
        for args in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.email", "t@example.com"],
            ["git", "config", "user.name", "T"],
        ):
            subprocess.run(args, cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-qm", "initial"], cwd=self.root, check=True, capture_output=True
        )
        Service.init_plane(self.root, feature_branch="feat/x", checks=self.checks)
        self.svc = Service(self.root)
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "recover",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": 4,
                "nodes": [
                    {"id": "w1", "label": "one", "dir": "src/a", "depends_on": [], "acceptance": ["true"]},
                    {"id": "w2", "label": "two", "dir": "src/b", "depends_on": [], "acceptance": ["true"]},
                ],
            },
        )
        unit = self.svc.create_campaign_workspace()
        self.worktree = Path(unit["worktree"])

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    git_calls: list = []

    def _track_git(self, worktree, *args, **kwargs):
        """A canned `git status` that reports a clean directory."""
        self.git_calls.append(args)

        from sliceme.gitutil import GitResult

        return GitResult(args=list(args), returncode=0, stdout="", stderr="")

    def write_tail(self, node, run_id="node-w1"):
        directory = campaign.progress_dir(self.root, "feat/x")
        directory.mkdir(parents=True, exist_ok=True)
        tail = directory / f"{node}.jsonl"
        tail.write_text(
            json_dumps(
                {
                    "node": node,
                    "step": 3,
                    "name": "edit x.py",
                    "tool": "edit",
                    "ts": 100.0,
                    "detail": "",
                    "run_id": run_id,
                }
            )
            + "\n",
            encoding="utf8",
        )
        return tail

    def test_recover_resets_one_node_directory_only(self):
        (self.worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        (self.worktree / "src" / "b" / "x.py").write_text("b = 2\n")
        self.write_tail("w1")
        result = surface.dispatch(self.svc, "recover", {"node": ["w1"]})
        self.assertEqual(result["restored"], ["src/a"])
        # The failed node's directory is back at HEAD; the sibling work is
        # untouched.
        self.assertEqual((self.worktree / "src" / "a" / "x.py").read_text(), "x = 1\n")
        self.assertEqual((self.worktree / "src" / "b" / "x.py").read_text(), "b = 2\n")
        # The tail rides the reply.
        self.assertEqual(result["nodes"][0]["tail"][0]["run_id"], "node-w1")
        self.assertEqual(result["nodes"][0]["tail"][0]["step"], 3)
        self.assertEqual(result["reason"], "ok")

    def test_recover_leaves_a_clean_directory_clean(self):
        result = surface.dispatch(self.svc, "recover", {"node": ["w1"]})
        self.assertEqual(result["restored"], [])
        self.assertEqual(result["nodes"][0]["restore"], "clean")
        self.assertEqual(result["nodes"][0]["tail"], [])

    def test_recover_refuses_a_running_child(self):
        # The workspace lock cannot see a live pi child, so the descriptor's
        # `running` mark is the one liveness signal: refuse the restore until
        # the child ends, and leave the work dirty.
        (self.worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        self.write_tail("w1")
        campaign.write_session(
            self.root,
            "feat/x",
            {"nodes": {"w1": {"status": "running", "stalls": 1}}},
        )
        with self.assertRaises(SlicemeError) as ctx:
            surface.dispatch(self.svc, "recover", {"node": ["w1"]})
        self.assertIn("marks it running", str(ctx.exception))
        self.assertEqual((self.worktree / "src" / "a" / "x.py").read_text(), "a = 2\n")

    def test_a_record_wins_over_a_stale_running_mark(self):
        # A record is durable evidence the node finished, so a stale
        # `running` mark from a crashed session cannot block the reset: the
        # verb reaches its git work (record commits the changes, so the
        # directory is clean and the reset is skipped).
        (self.worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        self.write_tail("w1")
        campaign.write_session(
            self.root,
            "feat/x",
            {"nodes": {"w1": {"status": "running", "stalls": 1}}},
        )
        self.svc.record_phase(0, messages={"w1": "change"})
        result = surface.dispatch(self.svc, "recover", {"node": ["w1"]})
        self.assertEqual(result["nodes"][0]["restore"], "clean")
        self.assertEqual(result["restored"], [])

    def test_a_clean_directory_skips_the_restore_call(self):
        # A clean directory restores nothing: the entry reports `clean` with
        # the progress row, and no `git restore` runs.
        with mock.patch(
            "sliceme.verbs.campaign.gitutil.git", side_effect=self._track_git
        ):
            result = surface.dispatch(self.svc, "recover", {"node": ["w1"]})
        self.assertEqual(len(self.git_calls), 1, "git ran status only, no restore")
        entry = result["nodes"][0]
        self.assertEqual(entry["restore"], "clean")
        self.assertEqual(result["restored"], [])
        self.assertIsNotNone(entry["progress"])
        self.assertEqual(entry["tail"], [])

    def test_failure_entries_carry_the_progress_row(self):
        # A `status` or `restore` failure keeps its error entry, and that
        # failure entry carries the tail and the progress row like a success.
        self.write_tail("w1")
        class FakeResult:
            def __init__(self, ok, stdout="", stderr=""):
                self.stdout = stdout
                self.stderr = stderr
                self.ok = ok

        calls = []

        def fake_git(worktree, *args, **kwargs):
            calls.append(args)
            if args[0] == "status":
                return FakeResult(False, stderr="git status exploded")
            return FakeResult(False, stderr="git restore exploded")

        with mock.patch(
            "sliceme.verbs.campaign.gitutil.git", side_effect=fake_git
        ):
            result = surface.dispatch(self.svc, "recover", {"node": ["w1"]})
        entry = result["nodes"][0]
        self.assertEqual(entry["restore"], "status-failed")
        self.assertEqual(entry["error"], "git status exploded")
        self.assertEqual(entry["tail"][0]["run_id"], "node-w1")
        self.assertIsNotNone(entry["progress"])
        self.assertEqual(calls, [("status", "--porcelain", "--", "src/a")])

        calls.clear()

        def fake_git_restore_fails(worktree, *args, **kwargs):
            calls.append(args)
            if args[0] == "status":
                return FakeResult(True, stdout="?? src/a/x.py\n")
            return FakeResult(False, stderr="git restore exploded")

        with mock.patch(
            "sliceme.verbs.campaign.gitutil.git", side_effect=fake_git_restore_fails
        ):
            result = surface.dispatch(self.svc, "recover", {"node": ["w1"]})
        entry = result["nodes"][0]
        self.assertEqual(entry["restore"], "restore-failed")
        self.assertEqual(entry["error"], "git restore exploded")
        self.assertEqual(entry["tail"][0]["run_id"], "node-w1")
        self.assertIsNotNone(entry["progress"])

    def test_recover_refuses_an_unknown_node(self):
        with self.assertRaises(SlicemeError) as ctx:
            surface.dispatch(self.svc, "recover", {"node": ["nope"]})
        self.assertIn("unknown node", str(ctx.exception))

    def test_recover_needs_a_node(self):
        with self.assertRaises(SlicemeError):
            surface.dispatch(self.svc, "recover", {})

    def test_recover_refuses_a_finished_campaign(self):
        row = self.svc.require_campaign()
        self.svc.store.set_campaign_state(row["key"], "delivered")
        (self.worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        with self.assertRaises(SlicemeError) as ctx:
            surface.dispatch(self.svc, "recover", {"node": ["w1"]})
        self.assertIn("the campaign is delivered", str(ctx.exception))
        self.assertEqual((self.worktree / "src" / "a" / "x.py").read_text(), "a = 2\n")

    def test_recover_refuses_a_root_node_restore(self):
        # A node that owns the whole tree cannot reset one directory: its
        # restore would span every sibling's directory too.
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "recover",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": 4,
                "nodes": [
                    {"id": "w0", "label": "root", "dir": ".", "depends_on": [], "acceptance": ["true"]},
                    {"id": "w1", "label": "one", "dir": "src/a", "depends_on": [], "acceptance": ["true"]},
                ],
            },
        )
        self.svc.normalize_dag()
        result = surface.dispatch(self.svc, "recover", {"node": ["w0"]})
        self.assertEqual(result["restored"], [])
        self.assertEqual(result["nodes"][0]["restore"], "root-node-refused")

    def test_recover_reports_a_node_with_no_owned_directory(self):
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "recover",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": 4,
                "nodes": [
                    {"id": "w1", "label": "one", "depends_on": [], "acceptance": ["true"]},
                ],
            },
        )
        self.svc.normalize_dag()
        result = surface.dispatch(self.svc, "recover", {"node": ["w1"]})
        self.assertEqual(result["restored"], [])
        self.assertEqual(result["nodes"][0]["restore"], "no-owned-directory")


def json_dumps(value):
    import json

    return json.dumps(value)


if __name__ == "__main__":
    unittest.main()
