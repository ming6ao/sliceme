"""The deterministic evidence document after the last wave.

``evidence`` writes ``.sliceme/<branch-key>.evidence.json`` (the complete
evidence) and ``.sliceme/<branch-key>.evidence.md`` (the check output is
bounded).  The document holds the campaign commits, the node details, the
newest check, the diffstat, the changed files, and the worker logs.  It is not
a gate.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign, surface
from sliceme.review import packet
from sliceme.service import Service
from sliceme.util import write_json


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class EvidenceCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("a = 1\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        Service.init_plane(self.root, feature_branch="feat/x", checks=self.checks)
        self.svc = Service(self.root)
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "evidence",
                "feature_branch": "feat/x",
                "base": "main",
                "design": "DESIGN.md",
                "nodes": [
                    {
                        "id": "w1",
                        "goal": "change a",
                        "owns": ["dir:src"],
                        "depends_on": [],
                        "acceptance": ["true"],
                    }
                ],
            },
        )

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def record(self):
        unit = self.svc.create_campaign_workspace()
        (Path(unit["worktree"]) / "src" / "a.py").write_text("a = 2\n")
        return self.svc.record_wave(0, messages={"w1": "change a"})

    def head(self):
        return self.svc.store.list_candidates()[0]["head_commit"]

    def write_check(self, output):
        return self.svc.store.create_check(
            fingerprint="fp-evidence",
            source="wave:0",
            commit_ref=self.head(),
            status="passed",
            commands=["true"],
            duration=1.5,
            output=output,
        )


class EvidenceTests(EvidenceCase):
    def test_document_holds_the_commits_and_the_checks(self):
        self.record()
        self.write_check("ok\n")
        result = self.svc.evidence()
        self.assertTrue(Path(result["path"]).is_file())
        self.assertTrue(Path(result["json_path"]).is_file())
        evidence = json.loads(Path(result["json_path"]).read_text())
        self.assertEqual(evidence["design"], "DESIGN.md")
        self.assertEqual(evidence["feature_branch"], "feat/x")
        self.assertEqual(evidence["delivery_base"], "main")
        self.assertEqual(evidence["report_path"], str(campaign.report_path(self.root, "feat/x")))
        self.assertEqual(len(evidence["commits"]), 1)
        commit = evidence["commits"][0]
        self.assertEqual(commit["node"], "w1")
        self.assertEqual(commit["goal"], "change a")
        self.assertEqual(commit["owns"], ["dir:src"])
        self.assertEqual(commit["check"]["status"], "passed")
        self.assertEqual(commit["check"]["commands"], ["true"])
        self.assertEqual(commit["check"]["fingerprint"], "fp-evidence")
        self.assertEqual(commit["check"]["duration"], 1.5)
        self.assertEqual(commit["check"]["output"], "ok\n")
        self.assertEqual(commit["diffstat"]["files"], 1)
        self.assertEqual(commit["diffstat"]["additions"], 1)
        self.assertEqual(commit["diffstat"]["deletions"], 1)
        self.assertEqual([row["path"] for row in commit["files"]], ["src/a.py"])
        self.assertTrue(commit["log"]["path"].endswith("feat--x.worker_w1.log"))
        # The narrative is deterministic and the engine writes it.
        self.assertIn("recorded 1 campaign commit(s)", result["content"])

    def test_markdown_bounds_the_check_output_and_the_json_keeps_it(self):
        self.record()
        long_output = "x" * (packet.MARKDOWN_OUTPUT_LIMIT + 500)
        self.write_check(long_output)
        result = self.svc.evidence()
        evidence = json.loads(Path(result["json_path"]).read_text())
        self.assertEqual(evidence["commits"][0]["check"]["output"], long_output)
        content = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn(f"{500} characters omitted", content)
        self.assertNotIn(long_output, content)

    def test_document_is_deterministic(self):
        self.record()
        self.write_check("ok\n")
        first = self.svc.evidence()
        second = self.svc.evidence()
        self.assertEqual(first["content"], second["content"])
        self.assertEqual(
            Path(first["json_path"]).read_text(),
            Path(second["json_path"]).read_text(),
        )

    def test_surface_dispatch_writes_the_evidence_document(self):
        self.record()
        result = surface.dispatch(self.svc, "evidence", {"design": "DESIGN.md"})
        self.assertEqual(Path(result["path"]).name, "feat--x.evidence.md")
        self.assertEqual(Path(result["json_path"]).name, "feat--x.evidence.json")
        self.assertTrue(Path(result["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
