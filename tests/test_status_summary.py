"""The compact status projection: the default human ``status`` output.

These tests pin :meth:`sliceme.service.Service.status_summary`, the dense
summary that mirrors the pi coordinator's ``summarise``: a header, one line per
node in DAG order, and one line per wave.  ``status --verbose`` keeps the old
nested dump, so the compact form is the default (docs/reference.md).
"""

import tempfile
import unittest
from pathlib import Path

from sliceme import campaign
from sliceme.service import Service
from sliceme.util import write_json


class StatusSummaryCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]
    nodes = [
        {"id": "w1", "owns": ["dir:src/a"], "depends_on": [], "acceptance": ["true"]},
        {"id": "w2", "owns": ["dir:src/b"], "depends_on": ["w1"], "acceptance": ["true"]},
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _git(self.root, "init", "-q", "-b", "main")
        _git(self.root, "config", "user.email", "t@example.com")
        _git(self.root, "config", "user.name", "Tester")
        for sub in ("a", "b"):
            (self.root / "src" / sub).mkdir(parents=True)
            (self.root / "src" / sub / "x.py").write_text("x = 1\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "initial")
        Service.init_plane(self.root, feature_branch="feat/x", checks=self.checks)
        self.svc = Service(self.root)
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "dense",
                "feature_branch": "feat/x",
                "base": "main",
                "design": "DESIGN.md",
                "concurrency": 2,
                "nodes": self.nodes,
            },
        )

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def test_summary_lines_match_the_reference_layout(self):
        write_json(
            campaign.state_path(self.root, "feat/x"),
            {
                "campaign": "dense",
                "target_branch": "feat/x",
                "worktree_branch": "feat/x",
                "base": "main",
                "wave_size": 2,
                "waves": [
                    {"index": 0, "members": ["w1"], "status": "done"},
                    {"index": 1, "members": ["w2"], "status": "pending"},
                ],
                "nodes": {"w1": {"status": "done"}, "w2": {"status": "running"}},
            },
        )
        view = self.svc.status_summary()
        self.assertEqual(view["campaign"], "dense")
        self.assertEqual(view["node_count"], 2)
        self.assertEqual(view["wave_size"], 2)
        self.assertEqual([n["id"] for n in view["nodes"]], ["w1", "w2"])
        self.assertEqual(view["lines"][0], "campaign: dense")
        self.assertEqual(
            view["lines"][1], "target:   feat/x  worktree: feat/x  base: main"
        )
        self.assertEqual(view["lines"][2], "design:   DESIGN.md")
        self.assertEqual(view["lines"][3], "nodes:    2  wave size: 2")
        self.assertEqual(view["lines"][4], "  w0 w1 [-] done")
        self.assertEqual(view["lines"][5], "  w1 w2 [-] running")
        self.assertEqual(view["lines"][6], "wave 0 [done]: w1")
        self.assertEqual(view["lines"][7], "wave 1 [pending]: w2")
        self.assertEqual(
            view["dag_waves"],
            [
                {"wave": 0, "status": "done", "members": ["w1"]},
                {"wave": 1, "status": "pending", "members": ["w2"]},
            ],
        )

    def test_wave_entries_accept_the_engine_wave_key(self):
        write_json(
            campaign.state_path(self.root, "feat/x"),
            {
                "wave_size": 1,
                "waves": [{"wave": 0, "members": ["w1", "w2"], "status": "running"}],
                "nodes": {"w1": {"status": "running"}, "w2": {"status": "pending"}},
            },
        )
        view = self.svc.status_summary()
        self.assertEqual([w["wave"] for w in view["dag_waves"]], [0])
        self.assertEqual(view["lines"][4], "  w0 w1 [-] running")
        self.assertEqual(view["lines"][5], "  w0 w2 [-] pending")
        self.assertEqual(view["lines"][6], "wave 0 [running]: w1, w2")

    def test_node_label_and_phase_are_rendered(self):
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "dense",
                "feature_branch": "feat/x",
                "nodes": [
                    {
                        "id": "w1",
                        "phase": "build",
                        "label": "the parser",
                        "owns": ["dir:src/a"],
                        "depends_on": [],
                    }
                ],
            },
        )
        view = self.svc.status_summary()
        self.assertEqual(view["lines"][4], "  w0 w1 [build] pending — the parser")

    def test_malformed_wave_entries_are_skipped(self):
        # A hand-edited state.json must not crash the default human status.
        write_json(
            campaign.state_path(self.root, "feat/x"),
            {
                "wave_size": 1,
                "waves": [
                    "not-a-dict",
                    {"index": 0, "members": ["w1"], "status": "done"},
                ],
                "nodes": {"w1": {"status": "done"}},
            },
        )
        view = self.svc.status_summary()
        self.assertEqual([w["members"] for w in view["dag_waves"]], [["w1"]])
        self.assertEqual(view["lines"][4], "  w0 w1 [-] done")

    def test_waves_fall_back_to_the_dag_plan(self):
        # No state.json waves yet: the engine's DAG projection fills the gap.
        view = self.svc.status_summary()
        self.assertEqual([w["members"] for w in view["dag_waves"]], [["w1"], ["w2"]])
        self.assertEqual([w["status"] for w in view["dag_waves"]], ["pending", "pending"])
        self.assertEqual(view["lines"][4], "  w0 w1 [-] pending")
        self.assertEqual(view["lines"][6], "wave 0 [pending]: w1")

    def test_missing_or_empty_dag_yields_a_header_only_summary(self):
        # A campaign with no DAG (missing file) never raises: it renders just
        # the header.  An empty `nodes` list behaves the same.
        campaign.dag_path(self.root, "feat/x").unlink()
        view = self.svc.status_summary()
        self.assertEqual(view["node_count"], 0)
        self.assertEqual(view["dag_waves"], [])
        self.assertEqual(len(view["lines"]), 4)
        self.assertEqual(view["lines"][3], "nodes:    0  wave size: ?")
        self.assertFalse(any(line.startswith("  w") for line in view["lines"]))
        self.assertFalse(any(line.startswith("wave ") for line in view["lines"]))

        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {"campaign": "dense", "feature_branch": "feat/x", "nodes": []},
        )
        view = self.svc.status_summary()
        self.assertEqual(view["node_count"], 0)
        self.assertEqual(view["dag_waves"], [])
        self.assertEqual(len(view["lines"]), 4)

    def test_plane_with_several_campaigns_summarizes_the_plane(self):
        self.svc.store.create_campaign(
            key="feat--y",
            target_branch="feat/y",
            worktree_branch="feat/y",
            base="main",
            unit_name="campaign:feat--y",
        )
        self.svc.store.conn.commit()
        self.svc.close()
        svc = Service(self.root)
        try:
            view = svc.status_summary()
        finally:
            svc.close()
        self.assertTrue(view.get("plane"))
        self.assertEqual(view["lines"][0], "campaigns: 2")
        self.assertEqual(
            {c["target_branch"] for c in view["campaigns"]}, {"feat/x", "feat/y"}
        )


def _git(root: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    )


if __name__ == "__main__":
    unittest.main()
