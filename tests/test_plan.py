"""Campaign plans: one design document, several sequential campaigns.

These pin the plan contract:

* the ``sliceme-campaigns`` fenced block parses into ordered entries;
* an absent block yields no plan;
* a malformed block raises a reason the caller can report;
* ``Service.campaign_plan`` joins the design order with the registry state and
  names the next campaign to run.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import plan
from sliceme.service import Service
from sliceme.util import SlicemeError


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


DESIGN = """# Design

Body.

```sliceme-campaigns
[
  {"name": "core", "target": "feat/core", "dirs": ["src", "include"]},
  {"name": "api", "target": "feat/api", "base": "feat/core", "dirs": ["python"]}
]
```

More body.
"""


class ParsePlanCase(unittest.TestCase):
    def test_absent_plan_is_empty(self):
        self.assertEqual(plan.parse_campaign_plan("# Design\n\nNo plan.\n"), [])

    def test_json_info_string(self):
        text = '```json sliceme-campaigns\n[{"name":"a","target":"feat/a"}]\n```\n'
        self.assertEqual(len(plan.parse_campaign_plan(text)), 1)

    def test_parses_entries_in_order(self):
        entries = plan.parse_campaign_plan(DESIGN)
        self.assertEqual([entry["name"] for entry in entries], ["core", "api"])
        self.assertEqual(entries[0]["dirs"], ["src", "include"])
        self.assertIsNone(entries[0]["base"])
        self.assertEqual(entries[1]["base"], "feat/core")

    def test_missing_name_fails(self):
        text = '```sliceme-campaigns\n[{"target":"feat/a"}]\n```\n'
        with self.assertRaises(SlicemeError):
            plan.parse_campaign_plan(text)

    def test_missing_target_fails(self):
        text = '```sliceme-campaigns\n[{"name":"a"}]\n```\n'
        with self.assertRaises(SlicemeError):
            plan.parse_campaign_plan(text)

    def test_duplicate_name_fails(self):
        text = (
            "```sliceme-campaigns\n"
            '[{"name":"a","target":"feat/a"},{"name":"a","target":"feat/b"}]\n'
            "```\n"
        )
        with self.assertRaises(SlicemeError):
            plan.parse_campaign_plan(text)

    def test_bad_dirs_fails(self):
        text = (
            "```sliceme-campaigns\n"
            '[{"name":"a","target":"feat/a","dirs":"src"}]\n'
            "```\n"
        )
        with self.assertRaises(SlicemeError):
            plan.parse_campaign_plan(text)

    def test_bad_json_fails(self):
        text = "```sliceme-campaigns\nnot json\n```\n"
        with self.assertRaises(SlicemeError):
            plan.parse_campaign_plan(text)


class ServicePlanCase(unittest.TestCase):
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
        (self.root / "DESIGN.md").write_text(DESIGN)
        self.svc = None

    def tearDown(self):
        if self.svc is not None:
            self.svc.close()
        self.tmp.cleanup()

    def plane(self):
        Service.init_plane(
            self.root,
            feature_branch="feat/other",
            base="main",
            checks=[{"name": "ok", "command": "true"}],
        )
        self.svc = Service(self.root)
        return self.svc

    def test_plan_without_campaigns_marks_all_pending(self):
        svc = self.plane()
        result = svc.campaign_plan("DESIGN.md")
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["next"], "core")
        self.assertEqual(
            [entry["state"] for entry in result["entries"]], ["pending", "pending"]
        )

    def test_plan_joins_registry_state(self):
        svc = self.plane()
        svc.store.create_campaign(
            key="feat--core",
            target_branch="feat/core",
            worktree_branch="feat/core",
            base="main",
            unit_name="campaign:feat--core",
            name="core",
        )
        svc.store.set_campaign_state("feat--core", "delivered")
        result = svc.campaign_plan("DESIGN.md")
        self.assertEqual(result["entries"][0]["state"], "delivered")
        self.assertEqual(result["next"], "api")
        self.assertEqual(result["entries"][1]["base"], "main")

    def test_missing_design_fails(self):
        svc = self.plane()
        with self.assertRaises(SlicemeError):
            svc.campaign_plan("NOPE.md")


if __name__ == "__main__":
    unittest.main()
