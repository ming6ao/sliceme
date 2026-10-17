"""Campaign orchestration core: the DAG, the phase recorder, and the report.

These pin the campaign contract:
* ``start`` records the campaign without creating a worktree;
* ``record_phase`` commits each node's owned paths on the one campaign worktree;
* ``report`` is a deterministic skeleton;
* the DAG projects into phases with a concurrency cap.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign
from sliceme.service import Service
from sliceme.util import SlicemeError, config_path, write_json


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class CampaignCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("a = 1\n")
        (self.root / "src" / "b.py").write_text("b = 1\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        self.svc = None

    def tearDown(self):
        if self.svc is not None:
            self.svc.close()
        self.tmp.cleanup()

    def campaign_plane(self, branch="feat/x", base="main"):
        # The campaign branch is the pull request head and the campaign worktree
        # branch.  The root stays on the delivery base, so the worktree can
        # check out the campaign branch.
        Service.init_plane(
            self.root, feature_branch=branch, base=base, checks=self.checks
        )
        self.svc = Service(self.root)
        return self.svc

    def dag(self, nodes, *, concurrency=2):
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "demo",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": concurrency,
                "nodes": nodes,
            },
        )

    def record(self, phase, edits):
        unit = self.svc.create_campaign_workspace()
        for rel, content in edits.items():
            (Path(unit["worktree"]) / rel).write_text(content)
        dag = campaign.load_dag(self.root, "feat/x")
        messages = {str(node["id"]): "test" for node in dag["nodes"]}
        return self.svc.record_phase(phase, messages=messages)

    def file_on(self, branch, rel):
        return run("git", "show", f"{branch}:{rel}", cwd=self.root).stdout

    def branch_exists(self, branch):
        return (
            subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                cwd=self.root,
                capture_output=True,
                text=True,
            ).returncode
            == 0
        )


class BootstrapTests(CampaignCase):
    def test_start_records_the_campaign_without_a_worktree(self):
        result = Service.init(self.root, design="DESIGN.md")
        self.assertEqual(result["work_branch"], "feat/design")
        self.assertIsNone(result["worktree"])
        service = Service(self.root)
        try:
            self.assertEqual(service.config["target_branch"], "feat/design")
            self.assertEqual(service.config["main_branch"], "feat/design")
            self.assertEqual(service.config["worktree_branch"], "feat/design")
            self.assertEqual(service.config["delivery_base"], "main")
            self.assertEqual(service.config["default_branch"], "main")
        finally:
            service.close()

    def test_init_uses_the_feature_branch_override(self):
        Service.init(self.root, feature_branch="feat/adopt")
        service = Service(self.root)
        try:
            self.assertEqual(service.config["target_branch"], "feat/adopt")
            self.assertEqual(service.config["delivery_base"], "main")
        finally:
            service.close()

    def test_init_needs_a_design_or_a_feature_branch(self):
        with self.assertRaises(SlicemeError):
            Service.init(self.root)

    def test_start_adds_a_campaign_for_a_second_feature_branch(self):
        Service.init(self.root, feature_branch="feat/x")
        Service.init(self.root, feature_branch="feat/y")
        service = Service(self.root)
        try:
            self.assertEqual(
                {c["work_branch"] for c in service.store.list_campaigns()},
                {"feat/x", "feat/y"},
            )
        finally:
            service.close()


class RecordPhaseTests(CampaignCase):
    def test_record_commits_each_node_on_the_campaign_branch(self):
        self.campaign_plane()
        # Two nodes may not share a directory in one phase; give them disjoint
        # ownership.
        self.dag(
            [
                {"id": "w1", "owns": ["src"], "depends_on": []},
                {"id": "w2", "owns": ["docs"], "depends_on": []},
            ]
        )
        unit = self.svc.create_campaign_workspace()
        (Path(unit["worktree"]) / "src" / "a.py").write_text("a = 2\n")
        (Path(unit["worktree"]) / "docs").mkdir(exist_ok=True)
        (Path(unit["worktree"]) / "docs" / "readme.md").write_text("docs\n")
        result = self.svc.record_phase(0, messages={"w1": "test", "w2": "test"})
        self.assertEqual([c["node"] for c in result["records"]], ["w1", "w2"])
        # The delivery base is untouched until the pull request merges.
        self.assertEqual(self.file_on("main", "src/a.py"), "a = 1\n")

    def test_record_rejects_a_path_outside_every_node(self):
        self.campaign_plane()
        self.dag([{"id": "w1", "owns": ["src"], "depends_on": []}])
        unit = self.svc.create_campaign_workspace()
        (Path(unit["worktree"]) / "README.md").write_text("# changed\n")
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.record_phase(0)
        self.assertIn("conformance failed", str(ctx.exception))


class ReportTests(CampaignCase):
    def test_report_is_deterministic(self):
        self.campaign_plane()
        self.dag(
            [
                {
                    "id": "w1",
                    "label": "alpha",
                    "owns": ["src"],
                    "depends_on": [],
                    "acceptance": ["true"],
                }
            ]
        )
        self.record(0, {"src/a.py": "a = 2\n"})
        first = self.svc.report(narrative="Did the thing.")
        second = self.svc.report(narrative="Did the thing.")
        self.assertEqual(first["content"], second["content"])
        self.assertIn("Feature branch: feat/x", first["content"])
        self.assertIn("w1", first["content"])
        self.assertIn("Did the thing.", first["content"])
        self.assertEqual(Path(first["path"]).name, "feat--x.report.md")

    def test_status_exposes_campaign_fields(self):
        self.campaign_plane()
        self.dag([{"id": "w1", "owns": ["src"], "depends_on": []}])
        self.record(0, {"src/a.py": "a = 2\n"})
        status = self.svc.status()
        self.assertEqual(status["work_branch"], "feat/x")
        self.assertEqual(status["default_branch"], "main")
        self.assertEqual(status["dag_path"], str(campaign.dag_path(self.root, "feat/x")))
        self.assertTrue(status["dag_phases"])

    def test_report_reads_the_campaign_state(self):
        # The report's per-node unit state is the campaign state, not a unit's.
        self.campaign_plane()
        self.dag([{"id": "w1", "owns": ["src"], "depends_on": []}])
        self.record(0, {"src/a.py": "a = 2\n"})
        self.svc.store.set_campaign_state("feat--x", "delivered")
        self.svc.store.conn.commit()
        report = self.svc.report(narrative="done")
        self.assertEqual(
            [row["unit_state"] for row in report["skeleton"]["nodes"]], ["delivered"]
        )


class DagPhaseStatusTests(CampaignCase):
    def test_status_projects_dag_phases_with_concurrency_cap(self):
        self.campaign_plane()
        self.dag(
            [
                {"id": "w1", "owns": ["a"], "depends_on": []},
                {"id": "w2", "owns": ["a/b"], "depends_on": []},
                {"id": "w3", "owns": ["c"], "depends_on": ["w1"]},
                {"id": "w4", "owns": ["d"], "depends_on": []},
            ]
        )
        status = self.svc.status()
        self.assertIsNone(status["dag_phases_error"])
        phases = status["dag_phases"]
        self.assertTrue(all(len(w["members"]) <= 2 for w in phases))
        assignment = {m: w["phase"] for w in phases for m in w["members"]}
        # w2 conflicts with w1 and w3 depends on w1, so both land in phase 1,
        # while w4 shares phase 0 with w1 within the concurrency cap.
        self.assertEqual(assignment, {"w1": 0, "w4": 0, "w2": 1, "w3": 1})

    def test_status_reports_a_cyclic_dag_without_crashing(self):
        self.campaign_plane()
        self.dag(
            [
                {"id": "w1", "owns": ["a"], "depends_on": ["w2"]},
                {"id": "w2", "owns": ["b"], "depends_on": ["w1"]},
            ]
        )
        status = self.svc.status()
        self.assertEqual(status["dag_phases"], [])
        self.assertIn("cycle", status["dag_phases_error"])


class ReadinessGateTests(CampaignCase):
    def record_node(self, node):
        """Insert a record so the phase cursor treats *node* as finished."""
        self.svc.store.create_record(
            head_commit="0" * 40, summary=None, node=node, campaign="feat--x"
        )
        self.svc.store.conn.commit()

    def write_descriptor(self, nodes, branch="feat/x"):
        campaign.write_session(
            self.root, branch, {"feature_branch": branch, "nodes": nodes}
        )

    def test_status_ready_gates_on_dependencies(self):
        self.campaign_plane()
        self.dag(
            [
                {"id": "w1", "owns": ["a"], "depends_on": []},
                {"id": "w2", "owns": ["b"], "depends_on": ["w1"]},
                {"id": "w3", "owns": ["c"], "depends_on": ["w1"]},
            ]
        )
        # w1 is recorded, so the cursor advances past phase 0 and its dependents
        # are ready; the phase is the one scheduling gate.
        self.record_node("w1")
        self.assertEqual(self.svc.ready_nodes(), ["w2", "w3"])
        self.assertEqual(self.svc.status()["ready"], ["w2", "w3"])

    def test_status_ready_excludes_recorded_and_running_nodes(self):
        self.campaign_plane()
        self.dag(
            [
                {"id": "w1", "owns": ["a"], "depends_on": []},
                {"id": "w2", "owns": ["b"], "depends_on": ["w1"]},
                {"id": "w3", "owns": ["c"], "depends_on": []},
            ]
        )
        # w1 is running and w3 is recorded, so nothing in phase 0 is ready; w2
        # waits on w1 in the next phase.
        self.record_node("w3")
        self.write_descriptor({"w1": {"status": "running"}})
        self.assertEqual(self.svc.ready_nodes(), [])

    def test_status_ready_needs_a_dag(self):
        self.campaign_plane()
        self.assertEqual(self.svc.ready_nodes(), [])
        self.assertEqual(self.svc.status()["ready"], [])


class DagStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_branch_key_replaces_slashes(self):
        self.assertEqual(campaign.branch_key("feat/nanochat-cpp"), "feat--nanochat-cpp")
        self.assertEqual(campaign.branch_key("main"), "main")

    def test_paths_share_one_glob_per_campaign(self):
        dag = campaign.dag_path(self.root, "feat/x")
        self.assertEqual(dag.name, "feat--x.dag.json")
        self.assertEqual(campaign.report_path(self.root, "feat/x").name, "feat--x.report.md")
        self.assertEqual(
            campaign.worker_log_path(self.root, "feat/x", "w1").name,
            "feat--x.worker_w1.log",
        )


class ConfigMigrationTests(CampaignCase):
    def test_default_branch_is_recorded_from_origin_head(self):
        # Simulate a remote default of ``main``.
        run("git", "update-ref", "refs/remotes/origin/main", "main", cwd=self.root)
        run(
            "git",
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
            cwd=self.root,
        )
        Service.init_plane(self.root, feature_branch="feat/x", checks=self.checks)
        cfg = json.loads(config_path(self.root).read_text())
        self.assertEqual(cfg["default_branch"], "main")
        self.assertEqual(cfg["base_branch"], "main")


class CampaignWorktreeTests(CampaignCase):
    def test_create_workspace_records_the_campaign_worktree(self):
        # The campaign owns one worktree; the engine records its path on the
        # campaign row so units never own it.
        self.campaign_plane()
        self.dag([{"id": "w1", "owns": ["src"], "depends_on": []}])
        unit = self.svc.create_campaign_workspace()
        row = self.svc.store.get_campaign("feat/x")
        self.assertEqual(row["worktree"], unit["worktree"])


class CurrentPhaseAdvanceTests(CampaignCase):
    def test_ready_advances_past_a_recorded_phase(self):
        # Nothing writes state.json, so the current phase is derived from the
        # recorded records in SQLite.  Before the fix this stayed on phase 0.
        self.campaign_plane()
        self.dag(
            [
                {"id": "w1", "owns": ["src"], "depends_on": []},
                {"id": "w2", "owns": ["docs"], "depends_on": ["w1"]},
            ]
        )
        before = self.svc.ready()
        self.assertEqual(before["phase"], 0)
        self.assertEqual(before["ready"], ["w1"])
        self.assertEqual(self.svc.current_phase_index(), 0)

        self.record(0, {"src/a.py": "a = 2\n"})

        after = self.svc.ready()
        self.assertEqual(after["phase"], 1)
        self.assertEqual(after["ready"], ["w2"])
        self.assertEqual(self.svc.current_phase_index(), 1)


if __name__ == "__main__":
    unittest.main()
