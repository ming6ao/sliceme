"""Pull request delivery: the forge call, the report body, and failure modes.

The local merge path is gone.  Delivery pushes the campaign worktree branch and
opens one pull request with ``gh``.  These tests pin the forge behavior, the
report-backed body, and the fail-closed rule: a push or forge failure leaves the
campaign working.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sliceme import campaign
from sliceme import pullrequest
from sliceme.service import Service
from sliceme.util import SlicemeError, write_json

FAKE_GH_BIN = Path(__file__).resolve().parent / "bin"


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class PullRequestCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    nodes = [
        {"id": "w1", "owns": ["dir:src/a"], "depends_on": [], "acceptance": ["true"]},
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
        run("git", "checkout", "-q", "-b", "feat/x", cwd=self.root)
        self.remote = Path(self.remote_tmp.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        run("git", "remote", "add", "origin", str(self.remote), cwd=self.root)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(FAKE_GH_BIN) + os.pathsep + self._old_path
        Service.init_plane(self.root, checks=self.checks)
        self.svc = Service(self.root)
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "pr",
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
        unit = self.svc.create_wave_workspace(0, base="feat/x")
        (Path(unit["worktree"]) / "src" / "a" / "x.py").write_text("a = 2\n")
        self.svc.record_wave(0, messages={"w1": "test"})
        return unit

    def approve(self):
        return self.svc.review_decision(action="approve", all_commits=True, actor="test")

    def campaign_row(self):
        return self.svc.store.get_campaign("feat/x")

    def pr_state(self, head):
        path = self.root / ".fake-gh.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text()).get("prs", {}).get(head, {})

    def remote_heads(self):
        return run("git", "ls-remote", "--heads", "origin", cwd=self.root).stdout

    def test_deliver_opens_pr_and_records_state(self):
        unit = self.record_one()
        self.approve()
        result = self.svc.deliver()
        self.assertEqual(result["results"][0]["status"], "landed")
        url = result["pull_request"]["url"]
        self.assertTrue(url.startswith("https://example.test/pull/"))
        row = self.campaign_row()
        self.assertEqual(row["pr_url"], url)
        self.assertEqual(row["state"], "delivered")
        # The target branch is unchanged until a human merges the pull request.
        self.assertEqual(
            run("git", "show", "feat/x:src/a/x.py", cwd=self.root).stdout, "a = 1\n"
        )
        # The campaign branch is pushed to origin.
        self.assertIn(unit["branch"], self.remote_heads())

    def test_pr_body_is_the_campaign_report(self):
        unit = self.record_one()
        self.approve()
        self.svc.deliver()
        pr = self.pr_state(unit["branch"])
        self.assertIn("Campaign report", pr["body"])
        self.assertIn(unit["branch"], pr["body"])
        self.assertEqual(pr["title"], "sliceme: pr")

    def test_missing_gh_is_a_clear_error(self):
        self.record_one()
        self.approve()
        with mock.patch("sliceme.pullrequest.available", return_value=False):
            with self.assertRaises(SlicemeError) as ctx:
                self.svc.deliver()
        self.assertIn("gh", str(ctx.exception))
        row = self.campaign_row()
        self.assertIsNone(row["pr_url"])
        self.assertEqual(row["state"], "working")

    def test_missing_remote_leaves_campaign_working(self):
        self.record_one()
        self.approve()
        run("git", "remote", "remove", "origin", cwd=self.root)
        with self.assertRaises(SlicemeError):
            self.svc.deliver()
        row = self.campaign_row()
        self.assertIsNone(row["pr_url"])
        self.assertEqual(row["state"], "working")

    def test_approval_gate_blocks_delivery(self):
        self.record_one()
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver()
        self.assertIn("not-approved", str(ctx.exception))
        self.assertIsNone(self.campaign_row()["pr_url"])

    def test_deliver_finds_gh_outside_the_process_path(self):
        # A process started from a desktop launcher or a service has a small
        # PATH. The GitHub CLI is installed, so delivery must still work.
        unit = self.record_one()
        self.approve()
        with mock.patch.object(pullrequest, "_which", return_value=None):
            with mock.patch.object(pullrequest, "_COMMON_BIN_DIRS", (str(FAKE_GH_BIN),)):
                with mock.patch.object(pullrequest, "_WINDOWS_GH_PATHS", ()):
                    result = self.svc.deliver()
        self.assertEqual(result["results"][0]["status"], "landed")
        self.assertTrue(result["pull_request"]["url"].startswith("https://example.test/pull/"))
        self.assertEqual(self.campaign_row()["state"], "delivered")
        self.assertIn(unit["branch"], self.remote_heads())

    def test_conflict_refuses_before_push(self):
        unit = self.record_one()
        # Advance the target branch with a conflicting change.
        (self.root / "src" / "a" / "x.py").write_text("a = 'target'\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "target change", cwd=self.root)
        self.approve()
        result = self.svc.deliver()
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertIn("conflict", result["results"][0]["detail"])
        self.assertIsNone(self.campaign_row()["pr_url"])
        self.assertNotIn(unit["branch"], self.remote_heads())


class GhResolverTests(unittest.TestCase):
    """``gh`` resolves from ``SLICEME_GH``, then ``PATH``, then common dirs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"PATH": ""})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SLICEME_GH", None)

    def fake_gh(self, name: str = "gh") -> Path:
        path = self.bin / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    def test_env_override_wins(self):
        program = self.fake_gh()
        os.environ["SLICEME_GH"] = str(program)
        with mock.patch.object(pullrequest, "_which", return_value=None), mock.patch.object(
            pullrequest, "_COMMON_BIN_DIRS", ()
        ), mock.patch.object(pullrequest, "_WINDOWS_GH_PATHS", ()):
            self.assertEqual(pullrequest.resolve(), str(program))

    def test_path_is_searched_first(self):
        program = self.fake_gh()
        os.environ["PATH"] = str(self.bin)
        with mock.patch.object(pullrequest, "_COMMON_BIN_DIRS", ()), mock.patch.object(
            pullrequest, "_WINDOWS_GH_PATHS", ()
        ):
            self.assertEqual(pullrequest.resolve(), str(program))

    def test_common_directory_is_a_fallback(self):
        # A minimal process PATH (desktop launcher, service) omits the
        # directory that holds the user-local `gh`.
        program = self.fake_gh()
        with mock.patch.object(pullrequest, "_which", return_value=None), mock.patch.object(
            pullrequest, "_COMMON_BIN_DIRS", (str(self.bin),)
        ), mock.patch.object(pullrequest, "_WINDOWS_GH_PATHS", ()):
            self.assertEqual(pullrequest.resolve(), str(program))

    def test_missing_program_resolves_to_none(self):
        with mock.patch.object(pullrequest, "_which", return_value=None), mock.patch.object(
            pullrequest, "_COMMON_BIN_DIRS", ()
        ), mock.patch.object(pullrequest, "_WINDOWS_GH_PATHS", ()):
            self.assertIsNone(pullrequest.resolve())

    def test_require_raises_the_clear_error(self):
        with mock.patch.object(pullrequest, "_which", return_value=None), mock.patch.object(
            pullrequest, "_COMMON_BIN_DIRS", ()
        ), mock.patch.object(pullrequest, "_WINDOWS_GH_PATHS", ()):
            with self.assertRaises(SlicemeError) as ctx:
                pullrequest.require()
        self.assertIn("SLICEME_GH", str(ctx.exception))
        self.assertIn("gh", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
