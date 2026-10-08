"""End-to-end: one design from ``start`` to the delivery pull request.

This pins the main-based campaign model.  ``start`` derives the campaign branch
from the design name and records the delivery base.  ``wave --open`` fetches the
delivery base and creates the campaign worktree on the campaign branch.  The
engine records and checks the wave, a human approves the campaign, the engine
writes the evidence document, and ``deliver`` pushes the campaign branch and
opens one pull request against the delivery base (``main``).

The flow is deterministic: the delivery base stays unchanged until a human
merges the pull request on the forge.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign
from sliceme.service import Service
from sliceme.util import write_json

FAKE_GH_BIN = Path(__file__).resolve().parent / "bin"


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class EndToEndCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote_tmp = tempfile.TemporaryDirectory()
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "src" / "api").mkdir(parents=True)
        (self.root / "src" / "api" / "app.py").write_text("value = 1\n")
        (self.root / "DESIGN.md").write_text("# Design\n\nAdd an endpoint.\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        self.remote = Path(self.remote_tmp.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        run("git", "remote", "add", "origin", str(self.remote), cwd=self.root)
        run("git", "push", "-q", "-u", "origin", "main", cwd=self.root)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(FAKE_GH_BIN) + os.pathsep + self._old_path
        self.svc = None

    def tearDown(self):
        if self.svc is not None:
            self.svc.close()
        os.environ["PATH"] = self._old_path
        self.remote_tmp.cleanup()
        self.tmp.cleanup()

    def start(self):
        """Run ``start`` and write the planner's DAG for the derived branch."""
        Service.init(
            self.root,
            design="DESIGN.md",
            base="main",
            checks=self.checks,
            no_unit=True,
        )
        self.svc = Service(self.root)
        write_json(
            campaign.dag_path(self.root, "feat/design"),
            {
                "campaign": "design",
                "feature_branch": "feat/design",
                "base": "main",
                "design": "DESIGN.md",
                "nodes": [
                    {
                        "id": "w1",
                        "goal": "add the endpoint",
                        "owns": ["dir:src/api"],
                        "depends_on": [],
                        "acceptance": ["true"],
                    }
                ],
            },
        )
        return self.svc

    def pr(self, head):
        path = self.root / ".fake-gh.json"
        return json.loads(path.read_text())["prs"][head]

    def test_the_full_flow_lands_one_pull_request(self):
        svc = self.start()
        # `start` derives the campaign branch and records the delivery base.
        self.assertEqual(svc.config["target_branch"], "feat/design")
        self.assertEqual(svc.config["worktree_branch"], "feat/design")
        self.assertEqual(svc.config["delivery_base"], "main")

        # `wave --open` fetches the delivery base and creates the campaign
        # worktree on the campaign branch.
        unit = svc.create_campaign_workspace()
        self.assertEqual(unit["branch"], "feat/design")
        self.assertNotIn("base_fallback", unit)
        (Path(unit["worktree"]) / "src" / "api" / "app.py").write_text("value = 2\n")

        recorded = svc.record_wave(0, messages={"w1": "add the endpoint"})
        self.assertEqual([c["node"] for c in recorded["candidates"]], ["w1"])

        check = svc.check_wave()
        self.assertEqual(check["status"], "passed")

        svc.review_decision(action="approve", all_commits=True, actor="reviewer")
        evidence = svc.evidence(design="DESIGN.md")
        self.assertTrue(Path(evidence["path"]).is_file())
        self.assertTrue(Path(evidence["json_path"]).is_file())
        self.assertIn("## Evidence", evidence["content"])

        result = svc.deliver()
        self.assertEqual([r["status"] for r in result["results"]], ["landed"])
        self.assertEqual(result["feature_branch"], "feat/design")
        pull = self.pr("feat/design")
        self.assertEqual(pull["base"], "main")
        self.assertEqual(pull["title"], "sliceme: design")
        self.assertIn("Campaign branch: `feat/design`", pull["body"])
        self.assertIn("Delivery base: `main`", pull["body"])

        # The delivery base is untouched until a human merges the pull request.
        self.assertEqual(
            run("git", "show", "main:src/api/app.py", cwd=self.root).stdout, "value = 1\n"
        )
        # The campaign branch carries the work and is pushed to the remote.
        self.assertEqual(
            run("git", "show", "feat/design:src/api/app.py", cwd=self.root).stdout,
            "value = 2\n",
        )
        self.assertIn(
            "feat/design",
            run("git", "ls-remote", "--heads", "origin", cwd=self.root).stdout,
        )
        self.assertEqual(svc.store.get_campaign("feat--design")["state"], "delivered")

        # Re-running delivery returns the same pull request, not a new one.
        again = svc.deliver()
        self.assertEqual(again["pull_request"]["url"], result["pull_request"]["url"])

    def test_a_failed_check_blocks_the_pull_request(self):
        svc = self.start()
        from sliceme.util import config_path

        cfg = json.loads(config_path(self.root).read_text())
        cfg["checks"] = [{"name": "needs-OK", "command": "test -f OK", "required": True}]
        config_path(self.root).write_text(json.dumps(cfg))

        unit = svc.create_campaign_workspace()
        (Path(unit["worktree"]) / "src" / "api" / "app.py").write_text("value = 2\n")
        svc.record_wave(0, messages={"w1": "add the endpoint"})
        svc.review_decision(action="approve", all_commits=True, actor="reviewer")

        result = svc.deliver()
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertIn("checks failed", result["results"][0]["detail"])
        self.assertFalse((self.root / ".fake-gh.json").exists())
        self.assertEqual(svc.store.get_campaign("feat--design")["state"], "working")


if __name__ == "__main__":
    unittest.main()
