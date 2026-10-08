"""The deterministic campaign report after the review cut.

``review --report`` writes ``.sliceme/<branch-key>.report.md``, and the review
packet includes that git-ignored file so a reviewer can read it.  The browser
client is gone, so the report is the remaining review artifact.

Pins ``docs/simplification-plan.md`` (Migration stage 2): "Keep ``review
--report``" and "keep ``campaign_branch_key`` and the report packet".
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign, surface
from sliceme.service import Service
from sliceme.util import write_json


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class ReportCase(unittest.TestCase):
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
        Service.init_plane(self.root, feature_branch="feat/x", checks=[{"name": "ok", "command": "true"}])
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
        self.tmp.cleanup()

    def record_wave0(self):
        unit = self.svc.create_campaign_workspace()
        (Path(unit["worktree"]) / "src" / "a" / "x.py").write_text("a = 2\n")
        (Path(unit["worktree"]) / "src" / "b" / "y.py").write_text("b = 2\n")
        return self.svc.record_wave(0, messages={"w1": "test", "w2": "test"})


class ReportTests(ReportCase):
    def test_report_is_written_and_included_in_the_packet(self):
        self.record_wave0()
        result = self.svc.report(narrative="the human story")
        self.assertTrue(Path(result["path"]).is_file())

        snapshot = self.svc.review_snapshot()
        self.assertTrue(snapshot["report"]["exists"])
        self.assertIn("the human story", snapshot["report"]["content"])
        self.assertEqual(snapshot["branch_key"], campaign.branch_key("feat/x"))

    def test_packet_drops_the_comment_lifecycle(self):
        self.record_wave0()
        snapshot = self.svc.review_snapshot()
        self.assertNotIn("comments", snapshot)

    def test_surface_dispatch_writes_the_report(self):
        self.record_wave0()
        result = surface.dispatch(
            self.svc,
            "review",
            {"report": True, "narrative": "landed", "design": "DESIGN.md"},
        )
        self.assertIn("landed", result["content"])
        self.assertTrue(Path(result["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
