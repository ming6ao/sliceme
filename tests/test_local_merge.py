"""Local delivery: merge the campaign branch into the local base branch.

``local`` delivery merges the campaign worktree branch into the local base
branch and stops.  Sliceme pushes nothing and calls no forge, so the user
pushes the base branch later.  These tests pin the merge commit, the ``landed``
state, the safety rails (a conflict, a dirty base, a remote that is ahead), and
the idempotent re-run.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sliceme import campaign
from sliceme.service import Service
from sliceme.util import SlicemeError, write_json

FAKE_GH_BIN = Path(__file__).resolve().parent / "bin"


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class LocalMergeCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    nodes = [
        {"id": "w1", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]},
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote_tmp = tempfile.TemporaryDirectory()
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "src" / "a").mkdir(parents=True)
        (self.root / "src" / "a" / "x.py").write_text("a = 1\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        self.remote = Path(self.remote_tmp.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        run("git", "remote", "add", "origin", str(self.remote), cwd=self.root)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(FAKE_GH_BIN) + os.pathsep + self._old_path
        Service.init_plane(
            self.root,
            feature_branch="feat/x",
            checks=self.checks,
            mode="local",
        )
        self.svc = Service(self.root)
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "local",
                "feature_branch": "feat/x",
                "base": "main",
                "nodes": self.nodes,
            },
        )

    def tearDown(self):
        self.svc.close()
        os.environ["PATH"] = self._old_path
        self.remote_tmp.cleanup()
        self.tmp.cleanup()

    def record_one(self):
        unit = self.svc.create_phase_workspace(0)
        (Path(unit["worktree"]) / "src" / "a" / "x.py").write_text("a = 2\n")
        self.svc.record_phase(0, messages={"w1": "test"})
        return unit

    def campaign_row(self):
        return self.svc.store.get_campaign("feat/x")

    def file_on(self, branch, rel):
        return run("git", "show", f"{branch}:{rel}", cwd=self.root).stdout

    def branch_exists(self, branch):
        return (
            subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                cwd=str(self.root),
                capture_output=True,
                text=True,
            ).returncode
            == 0
        )

    def test_local_merge_lands_on_the_base_branch(self):
        unit = self.record_one()
        result = self.svc.deliver(confirm=True)
        self.assertEqual(result["mode"], "local")
        self.assertEqual(result["results"][0]["status"], "delivered")
        self.assertIsNone(result["pull_request"])
        self.assertTrue(result["merge_commit"])
        # The change is on the local base branch and no pull request exists.
        self.assertEqual(self.file_on("main", "src/a/x.py"), "a = 2\n")
        row = self.campaign_row()
        self.assertEqual(row["state"], "landed")
        self.assertEqual(row["merge_commit"], result["merge_commit"])
        self.assertIsNone(row["pr_url"])
        # The campaign branch stays for provenance until the user pushes.
        self.assertTrue(self.branch_exists("feat/x"))
        # Nothing was pushed to the remote.
        remote = run("git", "ls-remote", "--heads", "origin", cwd=self.root).stdout
        self.assertNotIn("feat/x", remote)
        self.assertNotIn("refs/heads/main", remote)
        self.assertEqual(unit["branch"], "feat/x")

    def test_local_merge_creates_a_merge_commit(self):
        self.record_one()
        self.svc.deliver(confirm=True)
        parents = run("git", "rev-list", "--parents", "-n", "1", "main", cwd=self.root)
        # A merge commit has three tokens: the commit and its two parents.
        self.assertEqual(len(parents.stdout.split()), 3)

    def test_conflict_refuses_before_the_merge(self):
        self.record_one()
        # Advance the local base branch with a conflicting change.
        (self.root / "src" / "a" / "x.py").write_text("a = 'base'\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "base change", cwd=self.root)
        result = self.svc.deliver(confirm=True)
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertIn("conflict", result["results"][0]["detail"])
        self.assertEqual(self.campaign_row()["state"], "working")
        self.assertIsNone(self.campaign_row()["merge_commit"])

    def test_a_dirty_base_worktree_refuses(self):
        self.record_one()
        (self.root / "src" / "a" / "x.py").write_text("uncommitted\n")
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver(confirm=True)
        self.assertIn("uncommitted", str(ctx.exception))
        self.assertEqual(self.campaign_row()["state"], "working")

    def test_a_remote_that_is_ahead_refuses(self):
        self.record_one()
        # Publish main, then add one commit on the remote that is not local.
        run("git", "push", "-q", "origin", "main", cwd=self.root)
        (self.root / "src" / "a" / "x.py").write_text("a = 'remote'\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "remote change", cwd=self.root)
        run("git", "push", "-q", "origin", "main", cwd=self.root)
        run("git", "reset", "--hard", "-q", "HEAD~1", cwd=self.root)
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver(confirm=True)
        self.assertIn("ahead", str(ctx.exception))
        self.assertEqual(self.campaign_row()["state"], "working")

    def test_local_merge_is_idempotent(self):
        self.record_one()
        first = self.svc.deliver(confirm=True)
        self.assertEqual(first["results"][0]["status"], "delivered")
        second = self.svc.deliver(confirm=True)
        self.assertEqual(second["results"][0]["status"], "delivered")
        self.assertIn("already merged", second["results"][0]["detail"])
        self.assertEqual(second["merge_commit"], first["merge_commit"])
        # One merge commit only.
        count = run("git", "rev-list", "--merges", "--count", "main", cwd=self.root)
        self.assertEqual(count.stdout.strip(), "1")

    def test_local_merge_requires_confirmation(self):
        self.record_one()
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver()
        self.assertEqual(ctx.exception.reason, "not_confirmed")
        self.assertEqual(self.campaign_row()["state"], "working")

    def test_local_merge_needs_no_gh(self):
        self.record_one()
        with mock.patch("sliceme.pullrequest.available", return_value=False):
            result = self.svc.deliver(confirm=True)
        self.assertEqual(result["results"][0]["status"], "delivered")
        self.assertEqual(self.file_on("main", "src/a/x.py"), "a = 2\n")

    def test_mode_override_wins_for_one_deliver(self):
        # The plane default is local; an explicit pull_request mode pushes.
        self.record_one()
        result = self.svc.deliver(confirm=True, mode="pull_request")
        self.assertEqual(result["mode"], "pull_request")
        self.assertTrue(result["pull_request"]["url"].startswith("https://example.test/pull/"))
        self.assertEqual(self.campaign_row()["state"], "delivered")
        self.assertEqual(self.file_on("main", "src/a/x.py"), "a = 1\n")
        remote = run("git", "ls-remote", "--heads", "origin", cwd=self.root).stdout
        self.assertIn("feat/x", remote)


class LocalMergeDefaultTests(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "a.txt").write_text("hi\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        self.svc = None

    def tearDown(self):
        if self.svc is not None:
            self.svc.close()
        self.tmp.cleanup()

    def test_the_plane_default_is_pull_request(self):
        Service.init_plane(self.root, feature_branch="feat/x", checks=self.checks)
        self.svc = Service(self.root)
        self.assertEqual(self.svc.config["delivery_mode"], "pull_request")

    def test_start_records_the_local_mode(self):
        Service.init_plane(
            self.root,
            feature_branch="feat/x",
            checks=self.checks,
            mode="local",
        )
        self.svc = Service(self.root)
        self.assertEqual(self.svc.config["delivery_mode"], "local")
        self.assertEqual(self.svc.require_campaign()["delivery_mode"], "local")

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(SlicemeError) as ctx:
            Service.init_plane(
                self.root,
                feature_branch="feat/x",
                checks=self.checks,
                mode="fast",
            )
        self.assertIn("delivery mode", str(ctx.exception))


class GcKeepsLandedBranchTests(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    nodes = [
        {"id": "w1", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]},
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote_tmp = tempfile.TemporaryDirectory()
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "src" / "a").mkdir(parents=True)
        (self.root / "src" / "a" / "x.py").write_text("a = 1\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        self.remote = Path(self.remote_tmp.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        run("git", "remote", "add", "origin", str(self.remote), cwd=self.root)
        Service.init_plane(
            self.root,
            feature_branch="feat/x",
            checks=self.checks,
            mode="local",
        )
        self.svc = Service(self.root)
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "local",
                "feature_branch": "feat/x",
                "base": "main",
                "nodes": self.nodes,
            },
        )

    def tearDown(self):
        self.svc.close()
        self.remote_tmp.cleanup()
        self.tmp.cleanup()

    def branch_exists(self, branch):
        return (
            subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                cwd=str(self.root),
                capture_output=True,
                text=True,
            ).returncode
            == 0
        )

    def test_gc_keeps_the_branch_until_the_push(self):
        unit = self.svc.create_phase_workspace(0)
        (Path(unit["worktree"]) / "src" / "a" / "x.py").write_text("a = 2\n")
        self.svc.record_phase(0, messages={"w1": "test"})
        self.svc.deliver(confirm=True)
        self.svc.gc()
        # The merge is local, so the branch stays as a second reference.
        self.assertTrue(self.branch_exists("feat/x"))
        # After the user pushes the base branch, gc drops the branch.
        run("git", "push", "-q", "origin", "main", cwd=self.root)
        self.svc.gc()
        self.assertFalse(self.branch_exists("feat/x"))


if __name__ == "__main__":
    unittest.main()
