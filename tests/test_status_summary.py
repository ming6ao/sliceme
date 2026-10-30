"""The compact status projection: the default human ``status`` output.

These tests pin :meth:`sliceme.service.Service.status_summary`, the dense
summary that mirrors the pi coordinator's ``summarise``: a header, one line per
node in DAG order, and one line per phase.  ``status --verbose`` keeps the old
nested dump, so the compact form is the default (docs/reference.md).
"""

import tempfile
import unittest
from pathlib import Path

from sliceme import campaign
from sliceme.service import Service
from sliceme.util import SlicemeError, write_json


class StatusSummaryCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]
    nodes = [
        {"id": "w1", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]},
        {"id": "w2", "owns": ["src/b"], "depends_on": ["w1"], "acceptance": ["true"]},
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
        # The phase plan comes from the DAG; the node status comes from the
        # record (w1) and the session descriptor (w2 running).
        self.svc.store.create_record(
            head_commit="0" * 40, summary=None, node="w1", campaign="feat--x"
        )
        self.svc.store.conn.commit()
        write_json(
            campaign.session_path(self.root, "feat/x"),
            {"feature_branch": "feat/x", "nodes": {"w2": {"status": "running"}}},
        )
        view = self.svc.status_summary()
        self.assertEqual(view["campaign"], "dense")
        self.assertEqual(view["node_count"], 2)
        self.assertEqual(view["phase_size"], 2)
        self.assertEqual(view["phase_count"], 2)
        self.assertEqual(view["current_phase"], 1)
        self.assertEqual([n["id"] for n in view["nodes"]], ["w1", "w2"])
        self.assertEqual(view["lines"][0], "campaign:    dense")
        self.assertEqual(
            view["lines"][1],
            "work branch: feat/x  worktree: (unset)  base: main",
        )
        self.assertEqual(view["lines"][2], "design:      DESIGN.md")
        self.assertEqual(view["lines"][3], "nodes:       2  phase size: 2")
        self.assertEqual(view["lines"][4], "phases:      2  current: 1")
        self.assertEqual(view["lines"][5], "  w0 w1 recorded")
        self.assertEqual(view["lines"][6], "  w1 w2 running")
        self.assertEqual(view["lines"][7], "phase 0 [done]: w1")
        self.assertEqual(view["lines"][8], "phase 1 [pending]: w2")
        self.assertEqual(
            view["dag_phases"],
            [
                {"phase": 0, "status": "done", "members": ["w1"]},
                {"phase": 1, "status": "pending", "members": ["w2"]},
            ],
        )

    def test_a_finished_campaign_marks_the_current_phase_as_done(self):
        # The cursor stays on the last phase when every phase is recorded, so
        # the header marks that state instead of implying work remains.
        for node in ("w1", "w2"):
            self.svc.store.create_record(
                head_commit="0" * 40, summary=None, node=node, campaign="feat--x"
            )
        self.svc.store.conn.commit()
        view = self.svc.status_summary()
        self.assertEqual(view["phase_count"], 2)
        self.assertEqual(view["current_phase"], 1)
        self.assertEqual(view["lines"][4], "phases:      2  current: 1 (done)")

    def test_node_label_is_rendered(self):
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "dense",
                "feature_branch": "feat/x",
                "nodes": [
                    {
                        "id": "w1",
                        "label": "the parser",
                        "owns": ["src/a"],
                        "depends_on": [],
                    }
                ],
            },
        )
        view = self.svc.status_summary()
        self.assertEqual(view["lines"][5], "  w0 w1 pending — the parser")

    def test_phases_fall_back_to_the_dag_plan(self):
        # No state.json phases yet: the engine's DAG projection fills the gap.
        view = self.svc.status_summary()
        self.assertEqual([w["members"] for w in view["dag_phases"]], [["w1"], ["w2"]])
        self.assertEqual([w["status"] for w in view["dag_phases"]], ["pending", "pending"])
        self.assertEqual(view["current_phase"], 0)
        self.assertEqual(view["lines"][4], "phases:      2  current: 0")
        self.assertEqual(view["lines"][5], "  w0 w1 pending")
        self.assertEqual(view["lines"][7], "phase 0 [pending]: w1")

    def test_ready_rejects_the_removed_gpu_field(self):
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "dense",
                "feature_branch": "feat/x",
                "nodes": [
                    {
                        "id": "w1",
                        "owns": ["src/a"],
                        "depends_on": [],
                        "gpu": "T1",
                    }
                ],
            },
        )
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.ready()
        self.assertIn("gpu", str(ctx.exception))

    def test_missing_or_empty_dag_yields_a_header_only_summary(self):
        # A campaign with no DAG (missing file) never raises: it renders just
        # the header.  An empty `nodes` list behaves the same.
        campaign.dag_path(self.root, "feat/x").unlink()
        view = self.svc.status_summary()
        self.assertEqual(view["node_count"], 0)
        self.assertEqual(view["dag_phases"], [])
        self.assertIsNone(view["current_phase"])
        self.assertEqual(len(view["lines"]), 5)
        self.assertEqual(view["lines"][3], "nodes:       0  phase size: ?")
        self.assertEqual(view["lines"][4], "phases:      0  current: ?")
        self.assertFalse(any(line.startswith("  w") for line in view["lines"]))
        self.assertFalse(any(line.startswith("phase ") for line in view["lines"]))

        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {"campaign": "dense", "feature_branch": "feat/x", "nodes": []},
        )
        view = self.svc.status_summary()
        self.assertEqual(view["node_count"], 0)
        self.assertEqual(view["dag_phases"], [])
        self.assertEqual(len(view["lines"]), 5)

    def test_plane_with_several_campaigns_summarizes_the_plane(self):
        self.svc.store.create_campaign(
            key="feat--y",
            work_branch="feat/y",
            base_branch="main",
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
