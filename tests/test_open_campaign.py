"""The campaign worktree starts from the fetched delivery base.

Phase 3 pins: ``create_campaign_workspace`` fetches the delivery base and bases
the worktree on ``origin/<delivery_base>``, so the worktree holds commits the
local default branch may not have.  When the remote or the delivery base is
absent, the engine falls back to the local delivery base and reports the
fallback.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme.service import Service
from sliceme.util import SlicemeError


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class OpenCampaignCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote_tmp = tempfile.TemporaryDirectory()
        self.clone_tmp = tempfile.TemporaryDirectory()
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
        run("git", "push", "-q", "-u", "origin", "main", cwd=self.root)
        Service.init_plane(self.root, feature_branch="feat/x", checks=self.checks)
        self.svc = Service(self.root)

    def tearDown(self):
        self.svc.close()
        self.clone_tmp.cleanup()
        self.remote_tmp.cleanup()
        self.tmp.cleanup()

    def advance_origin_main(self) -> str:
        """Advance ``origin/main`` from a second clone; return the new commit.

        The original clone never sees the push, so its
        ``refs/remotes/origin/main`` stays stale and the worktree can only hold
        the commit after ``create_campaign_workspace`` fetches it.
        """
        clone = Path(self.clone_tmp.name)
        run("git", "clone", "-q", "-b", "main", str(self.remote), str(clone), cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=clone)
        run("git", "config", "user.name", "Tester", cwd=clone)
        (clone / "remote-only.txt").write_text("from the remote\n")
        run("git", "add", "-A", cwd=clone)
        run("git", "commit", "-qm", "remote only", cwd=clone)
        run("git", "push", "-q", "origin", "main", cwd=clone)
        return run("git", "rev-parse", "HEAD", cwd=clone).stdout.strip()

    def test_the_worktree_comes_from_origin_main(self):
        pushed = self.advance_origin_main()
        # The local remote-tracking ref is stale, so the worktree needs a fetch.
        stale = run(
            "git", "rev-parse", "refs/remotes/origin/main", cwd=self.root
        ).stdout.strip()
        self.assertNotEqual(stale, pushed)
        self.assertFalse((self.root / "remote-only.txt").exists())
        unit = self.svc.create_campaign_workspace()
        self.assertTrue((Path(unit["worktree"]) / "remote-only.txt").is_file())
        self.assertNotIn("base_fallback", unit)

    def test_a_missing_remote_falls_back_to_the_local_base(self):
        run("git", "remote", "remove", "origin", cwd=self.root)
        unit = self.svc.create_campaign_workspace()
        self.assertFalse((Path(unit["worktree"]) / "remote-only.txt").exists())
        self.assertIn("using local main", unit["base_fallback"])

    def commit_on_branch(self, branch, filename, content, *, create=False):
        """Commit one file on *branch* through a temporary worktree."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wt"
            args = ["git", "worktree", "add", "-q"]
            if create:
                args += ["-b", branch, str(path), "main"]
            else:
                args += [str(path), branch]
            run(*args, cwd=self.root)
            run("git", "config", "user.email", "t@example.com", cwd=path)
            run("git", "config", "user.name", "Tester", cwd=path)
            (path / filename).write_text(content)
            run("git", "add", "-A", cwd=path)
            run("git", "commit", "-qm", filename, cwd=path)
            tip = run("git", "rev-parse", "HEAD", cwd=path).stdout.strip()
            run("git", "worktree", "remove", "--force", str(path), cwd=self.root)
            return tip

    def test_a_stale_branch_is_fast_forwarded(self):
        run("git", "branch", "feat/x", "main", cwd=self.root)
        pushed = self.advance_origin_main()
        unit = self.svc.create_campaign_workspace()
        self.assertTrue((Path(unit["worktree"]) / "remote-only.txt").is_file())
        tip = run("git", "rev-parse", "feat/x", cwd=self.root).stdout.strip()
        self.assertEqual(tip, pushed)

    def test_an_ahead_branch_is_accepted(self):
        tip = self.commit_on_branch("feat/x", "ahead.txt", "ahead\n", create=True)
        unit = self.svc.create_campaign_workspace()
        self.assertTrue((Path(unit["worktree"]) / "ahead.txt").is_file())
        self.assertEqual(
            run("git", "rev-parse", "feat/x", cwd=self.root).stdout.strip(), tip
        )

    def test_a_diverged_branch_is_refused(self):
        self.commit_on_branch("feat/x", "branch-only.txt", "branch\n", create=True)
        self.advance_origin_main()
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.create_campaign_workspace()
        self.assertEqual(getattr(ctx.exception, "reason", None), "diverged_branch")
        self.assertIn("diverged", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
