"""The one phase cursor.

The ``ready``, ``record``, and ``check`` verbs all read one position.  A phase
advances when a member records zero changes (an explicit empty marker).  A
recorded node satisfies a dependency without a ``state.json`` writer.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign, gitutil
from sliceme.service import Service
from sliceme.util import SlicemeError, write_json


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class PhaseCursorCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        for sub in ("a", "b", "c"):
            (self.root / "src" / sub).mkdir(parents=True)
            (self.root / "src" / sub / "x.py").write_text("x = 1\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        Service.init_plane(self.root, feature_branch="feat/x", checks=self.checks)
        self.svc = Service(self.root)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def dag(self, nodes, *, concurrency=3):
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "cursor",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": concurrency,
                "nodes": nodes,
            },
        )

    def edit(self, rel, content):
        unit = self.svc.create_campaign_workspace()
        (Path(unit["worktree"]) / rel).write_text(content)

    def test_one_cursor_drives_ready_record_and_check(self):
        self.dag(
            [
                {"id": "w1", "label": "one", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]},
                {"id": "w2", "label": "two", "owns": ["src/b"], "depends_on": ["w1"], "acceptance": ["true"]},
            ]
        )
        before = self.svc.ready()
        self.assertEqual(before["phase"], 0)
        self.assertEqual(before["ready"], ["w1"])
        self.assertEqual(self.svc._phase_cursor("feat/x").index, 0)

        self.edit("src/a/x.py", "a = 2\n")
        self.svc.record_phase(0, messages={"w1": "change"})

        # The recorded position names the phase check runs, not the next phase.
        cursor = self.svc._phase_cursor("feat/x")
        self.assertEqual(cursor.recorded_index, 0)
        self.assertEqual(cursor.index, 1)
        checked = self.svc.check_phase()
        self.assertEqual(checked["phase"], cursor.recorded_index)
        self.assertEqual(checked["status"], "passed")

        after = self.svc.ready()
        self.assertEqual(after["phase"], cursor.index)
        self.assertEqual(after["ready"], ["w2"])

    def test_phase_advances_when_a_member_records_zero_changes(self):
        # w1 and w2 share phase 0; w3 waits on w1 in phase 1.  w2 changes no file,
        # so it records an explicit empty marker and phase 0 still completes.
        self.dag(
            [
                {"id": "w1", "label": "one", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]},
                {"id": "w2", "owns": ["src/b"], "depends_on": [], "acceptance": ["true"]},
                {"id": "w3", "owns": ["src/c"], "depends_on": ["w1"], "acceptance": ["true"]},
            ]
        )
        self.assertEqual(self.svc.ready()["ready"], ["w1", "w2"])

        self.edit("src/a/x.py", "a = 2\n")
        recorded = self.svc.record_phase(0, messages={"w1": "change", "w2": "change"})
        self.assertEqual(recorded["empty"], ["w2"])
        self.assertEqual([c["node"] for c in recorded["records"]], ["w1"])

        after = self.svc.ready()
        self.assertEqual(after["phase"], 1)
        self.assertEqual(after["ready"], ["w3"])

    def test_a_recorded_node_satisfies_a_dependency(self):
        # No state.json is written; w2 becomes ready only because w1's record
        # finishes phase 0.
        self.dag(
            [
                {"id": "w1", "label": "one", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]},
                {"id": "w2", "label": "two", "owns": ["src/b"], "depends_on": ["w1"], "acceptance": ["true"]},
            ]
        )
        self.assertEqual(self.svc.ready()["ready"], ["w1"])
        self.edit("src/a/x.py", "a = 2\n")
        self.svc.record_phase(0, messages={"w1": "change"})
        self.assertEqual(self.svc.ready()["ready"], ["w2"])

    def test_ready_carries_the_worktree_and_the_node_definition(self):
        self.dag(
            [
                {
                    "id": "w1",
                    "label": "A node",
                    "goal": "Change the file",
                    "tasks": ["first", "second"],
                    "owns": ["src/a"],
                    "depends_on": [],
                    "acceptance": ["true"],
                }
            ]
        )
        self.assertIsNone(self.svc.ready()["worktree"])
        unit = self.svc.create_campaign_workspace()
        reply = self.svc.ready()
        self.assertEqual(reply["worktree"], unit["worktree"])
        self.assertEqual(len(reply["nodes"]), 1)
        node = reply["nodes"][0]
        self.assertEqual(node["id"], "w1")
        self.assertEqual(node["dir"], "src/a")
        self.assertEqual(node["label"], "A node")
        self.assertEqual(node["goal"], "Change the file")
        self.assertEqual(node["tasks"], ["first", "second"])

    def test_check_phase_selects_one_recorded_phase(self):
        self.dag(
            [
                {"id": "w1", "label": "one", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]},
                {"id": "w2", "label": "two", "owns": ["src/b"], "depends_on": ["w1"], "acceptance": ["true"]},
            ]
        )
        self.edit("src/a/x.py", "a = 2\n")
        self.svc.record_phase(0)
        first = self.svc.store.list_records(campaign=self.svc.campaign_key())[0]
        self.edit("src/b/x.py", "b = 2\n")
        self.svc.record_phase(1)
        result = self.svc.check_phase(phase=0)
        self.assertEqual(result["phase"], 0)
        self.assertEqual(result["members"], ["w1"])
        self.assertEqual(result["commit"], first["head_commit"])
        self.assertEqual(result["commit_ref"], first["head_commit"])

    def test_check_refresh_bypasses_the_cache(self):
        self.dag(
            [{"id": "w1", "label": "one", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]}]
        )
        self.edit("src/a/x.py", "a = 2\n")
        self.svc.record_phase(0)
        first = self.svc.check_phase(phase=0)
        self.assertFalse(first["cached"])
        cached = self.svc.check_phase(phase=0)
        self.assertTrue(cached["cached"])
        fresh = self.svc.check_phase(phase=0, refresh=True)
        self.assertFalse(fresh["cached"])

    def test_reopen_drops_later_records_and_resets_the_worktree(self):
        self.dag(
            [
                {"id": "w1", "label": "one", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]},
                {"id": "w2", "label": "two", "owns": ["src/b"], "depends_on": ["w1"], "acceptance": ["true"]},
            ]
        )
        base = gitutil.rev_parse(self.root, "main")
        self.edit("src/a/x.py", "a = 2\n")
        self.svc.record_phase(0)
        self.edit("src/b/x.py", "b = 2\n")
        self.svc.record_phase(1)
        result = self.svc.reopen_phase(0)
        self.assertEqual(result["reopened"], 0)
        self.assertEqual(result["dropped_records"], 2)
        self.assertEqual(result["reset_to"], base)
        records = self.svc.store.list_records(campaign=self.svc.campaign_key())
        self.assertEqual(records, [])
        self.assertEqual(gitutil.head_commit(self.svc.campaign_worktree()), base)
        self.assertEqual(self.svc._phase_cursor("feat/x").index, 0)

    def test_reopen_refuses_a_dirty_worktree(self):
        self.dag(
            [{"id": "w1", "label": "one", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]}]
        )
        self.edit("src/a/x.py", "a = 2\n")
        self.svc.record_phase(0)
        (self.svc.campaign_worktree() / "src" / "a" / "x.py").write_text("dirty\n")
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.reopen_phase(0)
        self.assertIn("uncommitted", str(ctx.exception))

    def test_reopen_refuses_a_finished_campaign(self):
        self.dag(
            [{"id": "w1", "label": "one", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]}]
        )
        self.edit("src/a/x.py", "a = 2\n")
        self.svc.record_phase(0)
        self.svc.store.set_campaign_state(self.svc.campaign_key(), "landed")
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.reopen_phase(0)
        self.assertIn("landed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
