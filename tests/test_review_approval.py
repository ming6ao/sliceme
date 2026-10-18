"""The campaign approval after the review cut.

The approval is folded into delivery: ``deliver`` records one campaign-level
approval and consumes it when the delivery lands, so there is no separate
approve step.  ``deliver`` refuses without the ``confirm`` token, and the token
stands for the one human confirmation of the whole campaign.  The browser
client and the comment lifecycle are gone, so the gate no longer inspects
comment state.
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
        {"id": "w1", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]},
        {"id": "w2", "owns": ["src/b"], "depends_on": [], "acceptance": ["true"]},
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

    def record_phase0(self):
        unit = self.svc.create_campaign_workspace()
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")
        self.edit(unit["worktree"], "src/b/y.py", "b = 2\n")
        return self.svc.record_phase(0, messages={"w1": "test", "w2": "test"})


class ApprovalGateTests(ApprovalCase):
    def test_deliver_refuses_without_confirmation(self):
        self.record_phase0()
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver()
        self.assertEqual(ctx.exception.reason, "not_confirmed")
        # A refused delivery records no approval and lands nothing.
        self.assertIsNone(self.svc.campaign_approval())
        self.assertEqual(self.svc.store.get_campaign("feat/x")["state"], "working")

    def test_confirm_delivers_and_records_one_campaign_approval(self):
        self.record_phase0()
        commits = [c["hash"] for c in self.svc.review_snapshot()["commits"]]
        self.assertGreaterEqual(len(commits), 2)
        delivered = self.svc.deliver(confirm=True, actor="test", note="looks good")
        self.assertEqual([r["status"] for r in delivered["results"]], ["delivered"])
        # The decision is campaign-level: it does not bind to one commit.
        decision = self.svc.campaign_approval()
        self.assertIsNone(decision["commit_hash"])
        self.assertEqual(decision["action"], "approve")
        self.assertEqual(decision["actor"], "test")
        self.assertEqual(decision["note"], "looks good")
        # A landed delivery consumes the decision it used.
        self.assertIsNotNone(decision["consumed_at"])
        self.assertFalse(self.svc.campaign_approved())

    def test_approve_is_no_longer_an_engine_action(self):
        self.assertNotIn("approve", {action.name for action in surface.ACTIONS})


class CommentVerbsAreGoneTests(ApprovalCase):
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
