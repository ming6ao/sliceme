"""The campaign approval gate after the review cut.

The review surface records exactly one campaign-level approval.  Delivery
proceeds only while the newest campaign decision is an unconsumed ``approve``
(or an ``override`` with a note).  The browser client and the comment lifecycle
are gone, so the gate no longer inspects comment state.

Pins ``docs/simplification-plan.md`` (Migration stage 2, "Requirements
relaxed"): "a human approves the campaign once before delivery".
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign, surface
from sliceme.service import Service
from sliceme.util import SlicemeError, write_json


FAKE_GH_BIN = Path(__file__).resolve().parent / "bin"


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class ApprovalCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    nodes = [
        {"id": "w1", "owns": ["dir:src/a"], "depends_on": [], "acceptance": ["true"]},
        {"id": "w2", "owns": ["dir:src/b"], "depends_on": [], "acceptance": ["true"]},
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        for sub in ("a", "b"):
            (self.root / "src" / sub).mkdir(parents=True)
        (self.root / "src" / "a" / "x.py").write_text("a = 1\n")
        (self.root / "src" / "b" / "y.py").write_text("b = 1\n")
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
                "campaign": "review",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": 2,
                "nodes": self.nodes,
            },
        )

    def tearDown(self):
        self.svc.close()
        os.environ["PATH"] = self._old_path
        self.remote_tmp.cleanup()
        self.tmp.cleanup()

    def edit(self, worktree, rel, content):
        (Path(worktree) / rel).write_text(content)

    def record_wave0(self):
        unit = self.svc.create_campaign_workspace()
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")
        self.edit(unit["worktree"], "src/b/y.py", "b = 2\n")
        return self.svc.record_wave(0, messages={"w1": "test", "w2": "test"})


class ApprovalGateTests(ApprovalCase):
    def test_deliver_refuses_without_approval(self):
        self.record_wave0()
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver()
        self.assertEqual(ctx.exception.reason, "not_approved")

    def test_one_approval_covers_the_whole_campaign(self):
        self.record_wave0()
        commits = [c["hash"] for c in self.svc.review_snapshot()["commits"]]
        self.assertGreaterEqual(len(commits), 2)
        # The decision is campaign-level: it does not bind to one commit.
        decision = self.svc.review_decision(action="approve", actor="test")
        self.assertIsNone(decision["commit_hash"])
        self.assertTrue(self.svc.campaign_approved())
        self.assertEqual(self.svc.unapproved_commits(), [])

    def test_approve_admits_delivery_and_consumes_the_decision(self):
        self.record_wave0()
        self.svc.review_decision(action="approve", actor="test")
        delivered = self.svc.deliver()
        self.assertEqual([r["status"] for r in delivered["results"]], ["landed"])
        self.assertIsNotNone(self.svc.campaign_decision()["consumed_at"])
        self.assertFalse(self.svc.campaign_approved())
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.require_all_approved()
        self.assertEqual(ctx.exception.reason, "not_approved")

    def test_override_admits_delivery_and_needs_a_note(self):
        self.record_wave0()
        with self.assertRaises(SlicemeError):
            self.svc.review_decision(action="override")
        self.svc.review_decision(action="override", note="accepted risk")
        delivered = self.svc.deliver()
        self.assertEqual([r["status"] for r in delivered["results"]], ["landed"])

    def test_request_changes_needs_a_note_and_supersedes_an_approval(self):
        self.record_wave0()
        self.svc.review_decision(action="approve", actor="test")
        with self.assertRaises(SlicemeError):
            self.svc.review_decision(action="request_changes")
        self.svc.review_decision(action="request_changes", note="not yet")
        self.assertFalse(self.svc.campaign_approved())
        self.assertTrue(self.svc.unapproved_commits())
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver()
        self.assertEqual(ctx.exception.reason, "not_approved")

    def test_surface_dispatch_records_one_decision(self):
        self.record_wave0()
        decision = surface.dispatch(
            self.svc, "review", {"decision": "approve", "actor": "test"}
        )
        self.assertEqual(decision["action"], "approve")
        self.assertIsNone(decision["commit_hash"])


class CommentVerbsAreGoneTests(ApprovalCase):
    def test_review_refuses_without_a_flag(self):
        with self.assertRaises(SlicemeError):
            surface.dispatch(self.svc, "review", {})

    def test_removed_comment_methods_are_absent(self):
        for name in (
            "review_comment",
            "review_reply",
            "review_mark_addressed",
            "review_resolve",
            "review_poll",
            "review_ack",
            "unaddressed_comments",
            "require_comments_addressed",
        ):
            self.assertFalse(hasattr(self.svc, name), name)


if __name__ == "__main__":
    unittest.main()
