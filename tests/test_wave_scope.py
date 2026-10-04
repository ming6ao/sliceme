"""Shared wave worktrees and the single recorder.

Phase 3 pins: one worktree/branch per wave, conformance-by-ownership across the
shared tree (per-node commits), rejection of unowned/ambiguous/cross-node
changes, and integration of the wave branch as a unit.
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


class WaveScopeCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    nodes = [
        {"id": "w1", "owns": ["dir:src/a"], "depends_on": [], "acceptance": ["true"]},
        {"id": "w2", "owns": ["dir:src/b"], "depends_on": [], "acceptance": ["true"]},
        {"id": "w3", "owns": ["dir:src/c"], "depends_on": ["w1"], "acceptance": ["true"]},
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "src" / "a").mkdir(parents=True)
        (self.root / "src" / "b").mkdir(parents=True)
        (self.root / "src" / "c").mkdir(parents=True)
        (self.root / "src" / "a" / "x.py").write_text("a = 1\n")
        (self.root / "src" / "b" / "y.py").write_text("b = 1\n")
        (self.root / "src" / "c" / "z.py").write_text("c = 1\n")
        (self.root / "README.md").write_text("# readme\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        run("git", "checkout", "-q", "-b", "feat/x", cwd=self.root)
        Service.init_plane(self.root, checks=self.checks)
        self.svc = Service(self.root)
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "waves",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": 3,
                "nodes": self.nodes,
            },
        )

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def edit(self, worktree, rel, content):
        path = Path(worktree) / rel
        path.write_text(content)

    def file_on(self, branch, rel):
        return run("git", "show", f"{branch}:{rel}", cwd=self.root).stdout

    def approve(self):
        return self.svc.review_decision(action="approve", all_commits=True, actor="test")


class RecordTests(WaveScopeCase):
    def test_shared_worktree_records_per_node_commits(self):
        unit = self.svc.create_wave_workspace(0, base="feat/x")
        self.assertEqual(unit["name"], "campaign")
        worktree = unit["worktree"]
        self.edit(worktree, "src/a/x.py", "a = 2\n")
        self.edit(worktree, "src/b/y.py", "b = 2\n")

        result = self.svc.record_wave(0, messages={"w1": "add a", "w2": "add b"})
        nodes = [c["node"] for c in result["candidates"]]
        self.assertEqual(nodes, ["w1", "w2"])
        self.assertTrue(all(c["unit_branch"] == unit["branch"] for c in result["candidates"]))
        # One commit per node on the shared campaign branch.  The subject is the
        # human description, never a wave prefix.
        log = run("git", "log", "--format=%s", f"feat/x..{unit['branch']}", cwd=self.root)
        subjects = log.stdout.splitlines()
        self.assertIn("add a", subjects)
        self.assertIn("add b", subjects)
        self.assertNotIn("wave", log.stdout)
        self.assertNotIn("w1:", log.stdout)

    def test_missing_description_is_rejected(self):
        unit = self.svc.create_wave_workspace(0, base="feat/x")
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.record_wave(0)
        message = str(ctx.exception)
        self.assertIn("w1", message)
        self.assertIn("no description", message)

    def test_path_outside_every_node_is_rejected(self):
        unit = self.svc.create_wave_workspace(0, base="feat/x")
        self.edit(unit["worktree"], "README.md", "# changed\n")
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.record_wave(0)
        self.assertIn("conformance failed", str(ctx.exception))

    def test_cross_node_rename_is_rejected(self):
        unit = self.svc.create_wave_workspace(0, base="feat/x")
        worktree = Path(unit["worktree"])
        run("git", "mv", "src/a/x.py", "src/b/x.py", cwd=worktree)
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.record_wave(0)
        self.assertIn("spans nodes", str(ctx.exception))

    def test_open_is_idempotent(self):
        first = self.svc.create_wave_workspace(0, base="feat/x")
        second = self.svc.create_wave_workspace(0, base="feat/x")
        self.assertEqual(first["id"], second["id"])

    def test_record_requires_a_workspace(self):
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.record_wave(0)
        self.assertIn("no workspace", str(ctx.exception))


class WaveIntegrationTests(WaveScopeCase):
    def test_campaign_worktree_delivers_as_one_unit(self):
        unit = self.svc.create_wave_workspace(0, base="feat/x")
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")
        self.edit(unit["worktree"], "src/b/y.py", "b = 2\n")
        self.svc.record_wave(0, messages={"w1": "test", "w2": "test"})
        # Nothing lands on the target branch until delivery.
        self.assertEqual(self.file_on("feat/x", "src/a/x.py"), "a = 1\n")
        self.approve()
        delivered = self.svc.deliver()
        statuses = [r["status"] for r in delivered["results"]]
        self.assertEqual(statuses, ["landed"])
        self.assertEqual(self.file_on("feat/x", "src/a/x.py"), "a = 2\n")
        self.assertEqual(self.file_on("feat/x", "src/b/y.py"), "b = 2\n")
        # The default is a --no-ff merge commit (two parents).
        parents = run("git", "rev-list", "--parents", "-n", "1", "feat/x", cwd=self.root)
        self.assertEqual(len(parents.stdout.split()), 3)

    def test_later_wave_reuses_the_same_worktree(self):
        first = self.svc.create_wave_workspace(0, base="feat/x")
        self.edit(first["worktree"], "src/a/x.py", "a = 2\n")
        self.edit(first["worktree"], "src/b/y.py", "b = 2\n")
        self.svc.record_wave(0, messages={"w1": "test", "w2": "test"})

        # The next wave reuses the same worktree; the previous wave's files are
        # still present, so no rebase or recreation is needed.
        second = self.svc.create_wave_workspace(1, base="feat/x")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual((Path(second["worktree"]) / "src/a/x.py").read_text(), "a = 2\n")
        self.edit(second["worktree"], "src/c/z.py", "c = 2\n")
        self.svc.record_wave(1, messages={"w3": "test"})

        # Still nothing on the target until delivery.
        self.assertEqual(self.file_on("feat/x", "src/a/x.py"), "a = 1\n")
        self.approve()
        self.svc.deliver()
        self.assertEqual(self.file_on("feat/x", "src/a/x.py"), "a = 2\n")
        self.assertEqual(self.file_on("feat/x", "src/c/z.py"), "c = 2\n")

    def test_deliver_refuses_the_default_branch(self):
        from sliceme.util import config_path

        # Point a plane at main and confirm delivery is refused with no override.
        self.svc.close()
        run("git", "checkout", "-q", "main", cwd=self.root)
        import shutil

        shutil.rmtree(self.root / ".sliceme", ignore_errors=True)
        Service.init_plane(self.root, checks=self.checks)
        self.svc = Service(self.root)
        self.assertEqual(self.svc.config["target_branch"], "main")
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver()
        self.assertIn("default branch", str(ctx.exception))

    def test_deliver_conflict_leaves_target_untouched(self):
        unit = self.svc.create_wave_workspace(0, base="feat/x")
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")
        self.edit(unit["worktree"], "src/b/y.py", "b = 2\n")
        self.svc.record_wave(0, messages={"w1": "test", "w2": "test"})
        # Advance the target branch with a conflicting change.
        target = self.svc.config["target_branch"]
        (self.root / "src" / "a" / "x.py").write_text("a = 'target'\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "target change", cwd=self.root)
        target_head = run("git", "rev-parse", target, cwd=self.root).stdout.strip()

        self.approve()
        delivered = self.svc.deliver()
        self.assertEqual(delivered["results"][0]["status"], "failed")
        self.assertIn("conflict", delivered["results"][0]["detail"])
        self.assertEqual(
            run("git", "rev-parse", target, cwd=self.root).stdout.strip(), target_head
        )

    def test_deliver_check_failure_resets_target(self):
        import json

        from sliceme.util import config_path

        cfg = json.loads(config_path(self.root).read_text())
        cfg["checks"] = [{"name": "needs-OK", "command": "test -f OK", "required": True}]
        config_path(self.root).write_text(json.dumps(cfg))

        unit = self.svc.create_wave_workspace(0, base="feat/x")
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")
        self.svc.record_wave(0, messages={"w1": "test"})
        target = self.svc.config["target_branch"]
        target_head = run("git", "rev-parse", target, cwd=self.root).stdout.strip()

        self.approve()
        delivered = self.svc.deliver()
        self.assertEqual(delivered["results"][0]["status"], "failed")
        self.assertIn("checks failed", delivered["results"][0]["detail"])
        self.assertEqual(
            run("git", "rev-parse", target, cwd=self.root).stdout.strip(), target_head
        )
        self.assertEqual(self.file_on(target, "src/a/x.py"), "a = 1\n")

    def test_report_lists_nodes_for_wave_scope(self):
        unit = self.svc.create_wave_workspace(0, base="feat/x")
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")
        self.svc.record_wave(0, messages={"w1": "test"})
        report = self.svc.report(narrative="wave work")
        self.assertEqual([row["node"] for row in report["skeleton"]["nodes"]], ["w1", "w2", "w3"])
        self.assertIn("wave work", report["content"])


class WaveCliTests(WaveScopeCase):
    def run_cli(self, args):
        import os
        import sys

        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "bin" / "sliceme"), *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            env=env,
        )

    def test_cli_open_and_record(self):
        opened = self.run_cli(["--json", "wave", "--open"])
        self.assertEqual(opened.returncode, 0, opened.stderr)
        worktree = json.loads(opened.stdout)["unit"]["worktree"]
        self.edit(worktree, "src/a/x.py", "a = 3\n")

        recorded = self.run_cli(
            [
                "--json",
                "wave",
                "--record",
                "--wave",
                "0",
                "--messages",
                '{"w1": "wire the loader"}',
            ]
        )
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        payload = json.loads(recorded.stdout)
        self.assertEqual([c["node"] for c in payload["candidates"]], ["w1"])
        branch = payload["branch"]
        log = run("git", "log", "--format=%s", f"feat/x..{branch}", cwd=self.root)
        self.assertEqual(log.stdout.strip(), "wire the loader")


if __name__ == "__main__":
    unittest.main()
