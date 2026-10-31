"""Pull request delivery: the forge call, the summary body, and failure modes.

The local merge path is gone.  Delivery pushes the campaign worktree branch and
opens one pull request with ``gh``.  These tests pin the forge behavior, the
summary description, and the fail-closed rule: a push or forge failure leaves
the campaign working.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sliceme import campaign
from sliceme import integrate
from sliceme import pullrequest
from sliceme.service import Service
from sliceme.util import SlicemeError, write_json

FAKE_GH_BIN = Path(__file__).resolve().parent / "bin"


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class PullRequestCase(unittest.TestCase):
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
        Service.init_plane(self.root, feature_branch="feat/x", checks=self.checks)
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
        unit = self.svc.create_phase_workspace(0)
        (Path(unit["worktree"]) / "src" / "a" / "x.py").write_text("a = 2\n")
        self.svc.record_phase(0, messages={"w1": "test"})
        return unit

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
        result = self.svc.deliver(confirm=True)
        self.assertEqual(result["results"][0]["status"], "delivered")
        url = result["pull_request"]["url"]
        self.assertTrue(url.startswith("https://example.test/pull/"))
        row = self.campaign_row()
        self.assertEqual(row["pr_url"], url)
        self.assertEqual(row["state"], "delivered")
        # The delivery base is unchanged until a human merges the pull request.
        self.assertEqual(
            run("git", "show", "main:src/a/x.py", cwd=self.root).stdout, "a = 1\n"
        )
        # The campaign branch is the pull request head; the base is the delivery
        # base (the default branch).
        self.assertEqual(unit["branch"], "feat/x")
        self.assertEqual(result["feature_branch"], "feat/x")
        self.assertIn(unit["branch"], self.remote_heads())
        self.assertEqual(self.pr_state(unit["branch"])["base"], "main")

    def test_pr_body_is_the_summary_and_the_verification(self):
        unit = self.record_one()
        self.svc.deliver(confirm=True)
        pr = self.pr_state(unit["branch"])
        body = pr["body"]
        # The body answers two questions: what changed, and how it was checked.
        self.assertIn("## Summary", body)
        self.assertIn("## What changed", body)
        self.assertIn("## Verification", body)
        self.assertIn("**deliver**: passed", body)
        self.assertIn("`true`", body)
        self.assertIn("Campaign branch: `feat/x`", body)
        self.assertIn("Delivery base: `main`", body)
        self.assertEqual(pr["title"], "sliceme: pr")

    def test_pr_body_carries_no_raw_evidence(self):
        # A reviewer reads the diff for the detail.  The goals, the tasks, the
        # check output, the worker logs, and the local paths stay out of the
        # description; the local evidence document keeps them.
        unit = self.record_one()
        self.svc.deliver(confirm=True)
        body = self.pr_state(unit["branch"])["body"]
        self.assertNotIn("## Evidence", body)
        self.assertNotIn("- Goal:", body)
        self.assertNotIn("- Tasks:", body)
        self.assertNotIn("- Changed files:", body)
        self.assertNotIn(".worker_", body)
        self.assertNotIn(".sliceme/", body)
        self.assertNotIn("src/a/x.py", body)
        self.assertNotIn("## Artifacts", body)
        # The complete record is still written beside the campaign.
        evidence = self.root / ".sliceme" / "feat--x.evidence.md"
        self.assertTrue(evidence.is_file())
        self.assertIn("## Evidence", evidence.read_text(encoding="utf-8"))

    def test_the_delivery_narrative_leads_the_pr_body(self):
        unit = self.record_one()
        summary = "Adds the second value and keeps the check green."
        self.svc.deliver(confirm=True, narrative=summary)
        body = self.pr_state(unit["branch"])["body"]
        self.assertIn(summary, body)
        # The digest stays, and the narrative follows it under one summary.
        self.assertIn("The campaign records 1 commit across 1 node", body)
        self.assertLess(body.index("The campaign records"), body.index(summary))
        self.assertLess(body.index(summary), body.index("## What changed"))

    def test_missing_gh_is_a_clear_error(self):
        self.record_one()
        with mock.patch("sliceme.pullrequest.available", return_value=False):
            with self.assertRaises(SlicemeError) as ctx:
                self.svc.deliver(confirm=True)
        self.assertIn("gh", str(ctx.exception))
        row = self.campaign_row()
        self.assertIsNone(row["pr_url"])
        self.assertEqual(row["state"], "working")

    def test_missing_remote_leaves_campaign_working(self):
        self.record_one()
        run("git", "remote", "remove", "origin", cwd=self.root)
        with self.assertRaises(SlicemeError):
            self.svc.deliver(confirm=True)
        row = self.campaign_row()
        self.assertIsNone(row["pr_url"])
        self.assertEqual(row["state"], "working")

    def test_delivery_requires_confirmation(self):
        self.record_one()
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver()
        self.assertEqual(ctx.exception.reason, "not_confirmed")
        self.assertIsNone(self.campaign_row()["pr_url"])

    def test_deliver_finds_gh_outside_the_process_path(self):
        # A process started from a desktop launcher or a service has a small
        # PATH. The GitHub CLI is installed, so delivery must still work.
        unit = self.record_one()
        with mock.patch.object(pullrequest, "_which", return_value=None):
            with mock.patch.object(pullrequest, "_COMMON_BIN_DIRS", (str(FAKE_GH_BIN),)):
                with mock.patch.object(pullrequest, "_WINDOWS_GH_PATHS", ()):
                    result = self.svc.deliver(confirm=True)
        self.assertEqual(result["results"][0]["status"], "delivered")
        self.assertTrue(result["pull_request"]["url"].startswith("https://example.test/pull/"))
        self.assertEqual(self.campaign_row()["state"], "delivered")
        self.assertIn(unit["branch"], self.remote_heads())

    def test_conflict_refuses_before_push(self):
        unit = self.record_one()
        # Advance the delivery base with a conflicting change.
        delivery_base = self.svc.config["delivery_base"]
        run("git", "checkout", "-q", delivery_base, cwd=self.root)
        (self.root / "src" / "a" / "x.py").write_text("a = 'base'\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "base change", cwd=self.root)
        result = self.svc.deliver(confirm=True)
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertIn("conflict", result["results"][0]["detail"])
        self.assertIsNone(self.campaign_row()["pr_url"])
        self.assertNotIn(unit["branch"], self.remote_heads())

    def test_deliver_refuses_the_default_branch(self):
        # The campaign branch is the pull request head; delivery must refuse it
        # when it is the default branch.
        config = dict(self.svc.config)
        config["work_branch"] = "main"
        with self.assertRaises(SlicemeError) as ctx:
            integrate.deliver_pull_request(self.svc.store, self.svc.root, config)
        self.assertIn("default branch", str(ctx.exception))

    def test_deliver_refuses_a_campaign_branch_equal_to_the_delivery_base(self):
        # An explicit --base may name a non-default branch.  The campaign branch
        # must not equal it, or the pull request would have head == base.
        run("git", "branch", "develop", "main", cwd=self.root)
        config = dict(self.svc.config)
        config["work_branch"] = "develop"
        config["base_branch"] = "develop"
        with self.assertRaises(SlicemeError) as ctx:
            integrate.deliver_pull_request(self.svc.store, self.svc.root, config)
        self.assertIn("delivery base", str(ctx.exception))


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
