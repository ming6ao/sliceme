"""The one phase cursor (S0/S1).

These pin the unified cursor from ``docs/terminology-and-simplification-plan.md``
(S0/S1): ``ready``, ``record``, and ``check`` all read one position; a phase
advances when a member records zero changes (an explicit empty marker); and a
recorded node satisfies a dependency without a ``state.json`` writer.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign
from sliceme.service import Service
from sliceme.util import write_json


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
                {"id": "w1", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]},
                {"id": "w2", "owns": ["src/b"], "depends_on": ["w1"], "acceptance": ["true"]},
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
                {"id": "w1", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]},
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
                {"id": "w1", "owns": ["src/a"], "depends_on": [], "acceptance": ["true"]},
                {"id": "w2", "owns": ["src/b"], "depends_on": ["w1"], "acceptance": ["true"]},
            ]
        )
        self.assertEqual(self.svc.ready()["ready"], ["w1"])
        self.edit("src/a/x.py", "a = 2\n")
        self.svc.record_phase(0, messages={"w1": "change"})
        self.assertEqual(self.svc.ready()["ready"], ["w2"])


if __name__ == "__main__":
    unittest.main()
