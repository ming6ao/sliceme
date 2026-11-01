"""The per-node try counts at ``record`` (``docs/progress-watchdog.md`` §3.3).

``record`` derives one node's try count from the progress channel's distinct
run ids (`node-w1`, a salvage retake `node-w1-try-2`), stores it on the
record row, and ``status``, ``report``, and ``resume`` read it back.  The
resource never passes the counts itself: the host authority authorizes exact
fixed commands only, so the derivation stays engine-owned.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign
from sliceme.service import Service
from sliceme.util import write_json


def event(node, step, ts, run_id="node-w1"):
    return {
        "node": node,
        "step": step,
        "name": "edit x.py",
        "tool": "edit",
        "ts": ts,
        "detail": "",
        "run_id": run_id,
    }


class NodeTriesCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src" / "a").mkdir(parents=True)
        (self.root / "src" / "b").mkdir(parents=True)
        (self.root / "src" / "a" / "x.py").write_text("x = 1\n")
        (self.root / "src" / "b" / "x.py").write_text("x = 1\n")
        for args in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.email", "t@example.com"],
            ["git", "config", "user.name", "T"],
        ):
            subprocess.run(args, cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-qm", "initial"], cwd=self.root, check=True, capture_output=True
        )
        Service.init_plane(self.root, feature_branch="feat/x", checks=self.checks)
        self.svc = Service(self.root)
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "watchdog",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": 4,
                "nodes": [
                    {"id": "w1", "label": "one", "dir": "src/a", "depends_on": [], "acceptance": ["true"]},
                    {"id": "w2", "label": "two", "dir": "src/b", "depends_on": ["w1"], "acceptance": ["true"]},
                ],
            },
        )
        unit = self.svc.create_campaign_workspace()
        self.worktree = Path(unit["worktree"])

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def write_tail(self, node, used):
        directory = campaign.progress_dir(self.root, "feat/x")
        directory.mkdir(parents=True, exist_ok=True)
        lines = [
            event(node, step, 100.0 + step, run_id)
            for step, run_id in enumerate(used, start=1)
        ]
        (directory / f"{node}.jsonl").write_text(
            "\n".join(json_dumps(item) for item in lines) + "\n", encoding="utf8"
        )

    def test_record_derives_the_try_count(self):
        # Two runs touched node w1: the first failed, the salvage retake
        # finished it. `record` counts both.
        self.write_tail("w1", ["node-w1", "node-w1-try-2"])
        (self.worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        result = self.svc.record_phase(0, messages={"w1": "change"})
        # w2 depends on w1, so phase 0 records w1 only.
        self.assertEqual(result["empty"], [])
        self.assertEqual(result["results"][0]["tries"], 2)
        record = self.svc.store.get_record(result["results"][0]["record"])
        self.assertEqual(record["tries"], 2)

    def test_a_node_without_events_counts_one_try(self):
        # A child that never wrote progress (its `sliceme_progress` tool
        # stays unused) is one try, never zero.
        self.write_tail("w1", ["node-w1"])
        (self.worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        result = self.svc.record_phase(0, messages={"w1": "change"})
        self.assertEqual(result["results"][0]["tries"], 1)

    def test_a_rotated_first_run_still_counts_two_tries(self):
        # The writer keeps only the newest tail slice: a busy first run can
        # rotate its own `run_id` out while the salvage retake's `-try-2`
        # key survives, so one distinct run id is still two tries.
        self.write_tail("w1", ["node-w1-try-2"])
        (self.worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        result = self.svc.record_phase(0, messages={"w1": "change"})
        self.assertEqual(result["results"][0]["tries"], 2)
        record = self.svc.store.get_record(result["results"][0]["record"])
        self.assertEqual(record["tries"], 2)

    def test_status_and_report_read_the_counts(self):
        self.write_tail("w1", ["node-w1", "node-w1-try-2"])
        # Mark stalls the way the budget tick does: in the descriptor.
        campaign.write_session(
            self.root,
            "feat/x",
            {
                "status": "suspended",
                "nodes": {
                    "w1": {"status": "running", "stalls": 3, "waited_ms": 685000.0}
                },
            },
        )
        (self.worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        self.svc.record_phase(0, messages={"w1": "change"})
        summary = self.svc.status_summary()
        row = next(n for n in summary["nodes"] if n["id"] == "w1")
        self.assertEqual(row["tries"], 2)
        self.assertEqual(row["stalls"], 3)
        self.assertEqual(row["waited_ms"], 685000.0)
        self.assertIn("stalls 3", "\n".join(summary["lines"]))
        self.assertIn("tries 2", "\n".join(summary["lines"]))
        report = self.svc.report()
        content = report["content"]
        self.assertIn("## Progress", content)
        self.assertIn("node w1: 2 tries, 3 stalls, waited 11m", content)
        skeleton = report["skeleton"]
        self.assertEqual(skeleton["progress"][0]["node"], "w1")

    def test_report_shows_no_marks_without_retries_or_stalls(self):
        (self.worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        self.svc.record_phase(0, messages={"w1": "change"})
        report = self.svc.report()
        self.assertIn("(no retry or stall marks)", report["content"])

    def test_resume_reconciles_the_try_count(self):
        self.write_tail("w1", ["node-w1", "node-w1-try-2"])
        (self.worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        self.svc.record_phase(0, messages={"w1": "change"})
        plan = self.svc.resume()
        self.assertEqual(plan["tries"]["w1"], 2)
        self.assertEqual(plan["tries"]["w2"], 1)
        self.assertEqual(plan["stalls"]["w1"], 0)


def json_dumps(value):
    import json

    return json.dumps(value)


if __name__ == "__main__":
    unittest.main()
