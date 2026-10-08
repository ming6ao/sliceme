"""Campaign orchestration core: the DAG, the wave recorder, and the report.

These pin the campaign contract:
* ``start --no-unit`` leaves no phantom unit and records the default branch;
* ``record_wave`` commits each node's owned paths on the one campaign worktree;
* ``report`` is a deterministic skeleton;
* the DAG projects into waves with a concurrency cap.
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

    def record(self, wave, edits):
        unit = self.svc.create_campaign_workspace()
        for rel, content in edits.items():
            (Path(unit["worktree"]) / rel).write_text(content)
        dag = campaign.load_dag(self.root, "feat/x")
        messages = {str(node["id"]): "test" for node in dag["nodes"]}
        return self.svc.record_wave(wave, messages=messages)

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


class NoUnitBootstrapTests(CampaignCase):
    def test_no_unit_leaves_no_phantom_unit_and_records_default(self):
        result = Service.init(self.root, design="DESIGN.md", no_unit=True)
        self.assertIsNone(result["unit"])
        self.assertIsNone(result["worktree"])
        service = Service(self.root)
        try:
            self.assertEqual(service.list_units(), [])
            self.assertEqual(service.config["target_branch"], "feat/design")
            self.assertEqual(service.config["main_branch"], "feat/design")
            self.assertEqual(service.config["worktree_branch"], "feat/design")
            self.assertEqual(service.config["delivery_base"], "main")
            self.assertEqual(service.config["default_branch"], "main")
        finally:
            service.close()

    def test_init_uses_the_feature_branch_override(self):
        Service.init(self.root, feature_branch="feat/adopt", no_unit=True)
        service = Service(self.root)
        try:
            self.assertEqual(service.config["target_branch"], "feat/adopt")
            self.assertEqual(service.config["delivery_base"], "main")
        finally:
            service.close()

    def test_init_needs_a_design_or_a_feature_branch(self):
        with self.assertRaises(SlicemeError):
            Service.init(self.root, no_unit=True)

    def test_start_adds_a_campaign_for_a_second_feature_branch(self):
        Service.init(self.root, feature_branch="feat/x", no_unit=True)
        Service.init(self.root, feature_branch="feat/y", no_unit=True)
        service = Service(self.root)
        try:
            self.assertEqual(
                {c["target_branch"] for c in service.store.list_campaigns()},
                {"feat/x", "feat/y"},
            )
        finally:
            service.close()


class RecordWaveTests(CampaignCase):
    def test_record_commits_each_node_on_the_campaign_branch(self):
        self.campaign_plane()
        # Two nodes may not share a directory in one wave; give them disjoint
        # ownership.
        self.dag(
            [
                {"id": "w1", "owns": ["dir:src"], "depends_on": []},
                {"id": "w2", "owns": ["dir:docs"], "depends_on": []},
            ]
        )
        unit = self.svc.create_campaign_workspace()
        (Path(unit["worktree"]) / "src" / "a.py").write_text("a = 2\n")
        (Path(unit["worktree"]) / "docs").mkdir(exist_ok=True)
        (Path(unit["worktree"]) / "docs" / "readme.md").write_text("docs\n")
        result = self.svc.record_wave(0, messages={"w1": "test", "w2": "test"})
        self.assertEqual([c["node"] for c in result["candidates"]], ["w1", "w2"])
        # The delivery base is untouched until the pull request merges.
        self.assertEqual(self.file_on("main", "src/a.py"), "a = 1\n")

    def test_record_rejects_a_path_outside_every_node(self):
        self.campaign_plane()
        self.dag([{"id": "w1", "owns": ["dir:src"], "depends_on": []}])
        unit = self.svc.create_campaign_workspace()
        (Path(unit["worktree"]) / "README.md").write_text("# changed\n")
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.record_wave(0)
        self.assertIn("conformance failed", str(ctx.exception))


class ReportTests(CampaignCase):
    def test_report_is_deterministic(self):
        self.campaign_plane()
        self.dag(
            [
                {
                    "id": "w1",
                    "label": "alpha",
                    "phase": "P0",
                    "owns": ["dir:src"],
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
        self.dag([{"id": "w1", "owns": ["dir:src"], "depends_on": []}])
        self.record(0, {"src/a.py": "a = 2\n"})
        status = self.svc.status()
        self.assertEqual(status["feature_branch"], "feat/x")
        self.assertEqual(status["default_branch"], "main")
        self.assertTrue(status["dag_waves"])


class DagWaveStatusTests(CampaignCase):
    def test_status_projects_dag_waves_with_concurrency_cap(self):
        self.campaign_plane()
        self.dag(
            [
                {"id": "w1", "owns": ["dir:a"], "depends_on": []},
                {"id": "w2", "owns": ["dir:a"], "depends_on": []},
                {"id": "w3", "owns": ["dir:c"], "depends_on": ["w1"]},
                {"id": "w4", "owns": ["dir:d"], "depends_on": []},
            ]
        )
        status = self.svc.status()
        self.assertIsNone(status["dag_waves_error"])
        waves = status["dag_waves"]
        self.assertTrue(all(len(w["members"]) <= 2 for w in waves))
        assignment = {m: w["wave"] for w in waves for m in w["members"]}
        # w2 conflicts with w1 and w3 depends on w1, so both land in wave 1,
        # while w4 shares wave 0 with w1 within the concurrency cap.
        self.assertEqual(assignment, {"w1": 0, "w4": 0, "w2": 1, "w3": 1})

    def test_status_reports_a_cyclic_dag_without_crashing(self):
        self.campaign_plane()
        self.dag(
            [
                {"id": "w1", "owns": ["dir:a"], "depends_on": ["w2"]},
                {"id": "w2", "owns": ["dir:b"], "depends_on": ["w1"]},
            ]
        )
        status = self.svc.status()
        self.assertEqual(status["dag_waves"], [])
        self.assertIn("cycle", status["dag_waves_error"])


class ReadinessGateTests(CampaignCase):
    def write_state(self, nodes, *, waves=None, current_wave=0):
        write_json(
            campaign.state_path(self.root, "feat/x"),
            {
                "campaign": "demo",
                "feature_branch": "feat/x",
                "waves": waves or [],
                "current_wave": current_wave,
                "nodes": nodes,
            },
        )

    def test_status_ready_gates_on_dependencies(self):
        self.campaign_plane()
        self.dag(
            [
                {"id": "w1", "owns": ["dir:a"], "depends_on": []},
                {"id": "w2", "owns": ["dir:b"], "depends_on": ["w1"]},
                {"id": "w3", "owns": ["dir:c"], "depends_on": ["w1"]},
            ]
        )
        # w1 is done, so its dependents are ready even though they share a wave
        # index; the gate is readiness, not wave membership (DEC-2).
        self.write_state({"w1": {"status": "done"}})
        self.assertEqual(self.svc.ready_nodes(), ["w2", "w3"])
        self.assertEqual(self.svc.status()["ready"], ["w2", "w3"])

    def test_status_ready_excludes_done_running_and_blocked_nodes(self):
        self.campaign_plane()
        self.dag(
            [
                {"id": "w1", "owns": ["dir:a"], "depends_on": []},
                {"id": "w2", "owns": ["dir:b"], "depends_on": ["w1"]},
                {"id": "w3", "owns": ["dir:c"], "depends_on": []},
            ]
        )
        self.write_state(
            {
                "w1": {"status": "running"},
                "w2": {"status": "pending"},
                "w3": {"status": "done"},
            }
        )
        # w2 waits on w1; w1 is running and w3 is done, so nothing is ready.
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
        self.assertEqual(campaign.state_path(self.root, "feat/x").name, "feat--x.state.json")
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
        self.assertEqual(cfg["delivery_base"], "main")


class CurrentWaveAdvanceTests(CampaignCase):
    def test_ready_advances_past_a_recorded_wave(self):
        # Nothing writes state.json, so the current wave is derived from the
        # recorded candidates in SQLite.  Before the fix this stayed on wave 0.
        self.campaign_plane()
        self.dag(
            [
                {"id": "w1", "owns": ["dir:src"], "depends_on": []},
                {"id": "w2", "owns": ["dir:docs"], "depends_on": ["w1"]},
            ]
        )
        before = self.svc.ready()
        self.assertEqual(before["wave"], 0)
        self.assertEqual(before["ready"], ["w1"])
        self.assertEqual(self.svc.current_wave_index(), 0)

        self.record(0, {"src/a.py": "a = 2\n"})

        after = self.svc.ready()
        self.assertEqual(after["wave"], 1)
        self.assertEqual(after["ready"], ["w2"])
        self.assertEqual(self.svc.current_wave_index(), 1)


if __name__ == "__main__":
    unittest.main()
