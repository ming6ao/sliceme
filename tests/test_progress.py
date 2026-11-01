"""The per-node child progress channel (``docs/progress-watchdog.md`` §3).

The pi package extension writes ``<node>.json`` and ``<node>.jsonl`` under
``.sliceme/<key>.progress/``; the engine only reads.  These tests write the
channel files the writer would produce and pin the reader verbs: ``progress``
rows, the ``ready`` brief fields, and the spend verdict the retry loop reads.
The recovery verbs live in ``tests/test_recover.py``; the try counts in
``tests/test_node_tries.py``.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign, progress, surface
from sliceme.service import Service
from sliceme.util import write_json


def event(node, step, ts, *, name="edit a.py", tool="edit", run_id="node-w1"):
    return {
        "node": node,
        "step": step,
        "name": name,
        "tool": tool,
        "ts": ts,
        "detail": "",
        "run_id": run_id,
    }


class ProgressChannelCase(unittest.TestCase):
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
                    {
                        "id": "w1",
                        "label": "one",
                        "dir": "src/a",
                        "depends_on": [],
                        "acceptance": ["true"],
                    },
                    {
                        "id": "w2",
                        "label": "two",
                        "dir": "src/b",
                        "depends_on": ["w1"],
                        "acceptance": ["true"],
                    },
                ],
            },
        )
        self.progress_dir = campaign.progress_dir(self.root, "feat/x")

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def write_channel(self, node, events):
        self.progress_dir.mkdir(parents=True, exist_ok=True)
        tail = self.progress_dir / f"{node}.jsonl"
        lines = [json.dumps(item) for item in events]
        tail.write_text("\n".join(lines) + "\n", encoding="utf8")
        (self.progress_dir / f"{node}.json").write_text(
            json.dumps(events[-1]) + "\n", encoding="utf8"
        )

    def test_progress_reports_the_channel_roots(self):
        tail = campaign.progress_tail_path(self.root, "feat/x", "w1")
        mirror = campaign.progress_event_path(self.root, "feat/x", "w1")
        self.assertEqual(tail.name, "w1.jsonl")
        self.assertEqual(mirror.name, "w1.json")
        self.assertIn("feat--x.progress", str(tail))

    def test_progress_rows_carry_the_writer_fields(self):
        self.write_channel("w1", [event("w1", 1, 100.0), event("w1", 2, 200.0)])
        report = self.svc.progress(now=200.5)
        self.assertEqual(report["campaign"], "feat--x")
        rows = {row["node"]: row for row in report["nodes"]}
        # One node wrote the channel; the other has no events yet.
        self.assertEqual(set(rows), {"w1", "w2"})
        w1 = rows["w1"]
        self.assertEqual(w1["step"], 2)
        self.assertEqual(w1["last_ts"], 200.0)
        self.assertEqual(w1["stalled_ms"], 500)
        self.assertFalse(w1["attention"])
        w2 = rows["w2"]
        self.assertIsNone(w2["step"])
        self.assertIsNone(w2["stalled_ms"])
        self.assertFalse(w2["attention"])

    def test_progress_marks_a_stalled_node(self):
        # A node silent for longer than the engine's stall threshold is
        # `attention` in the reader rows and the brief `ready` fields alike.
        self.write_channel("w1", [event("w1", 9, 100.0)])
        report = self.svc.progress(now=100.0 + progress.STALL_AFTER_SECONDS + 5)
        row = report["nodes"][0]
        self.assertTrue(row["attention"])
        self.assertGreater(row["stalled_ms"], progress.STALL_AFTER_SECONDS * 1000)

    def test_progress_can_limit_to_named_nodes(self):
        self.write_channel("w2", [event("w2", 3, 10.0, run_id="node-w2")])
        report = self.svc.progress(nodes=["w2"], now=11.0)
        self.assertEqual([row["node"] for row in report["nodes"]], ["w2"])
        self.assertEqual(report["nodes"][0]["step"], 3)

    def test_empty_rows_for_a_missing_channel(self):
        report = self.svc.progress(now=1.0)
        self.assertEqual([row["node"] for row in report["nodes"]], ["w1", "w2"])
        for row in report["nodes"]:
            self.assertIsNone(row["step"])
            self.assertFalse(row["attention"])

    def test_ready_brief_carries_the_progress_fields(self):
        self.svc.create_campaign_workspace()
        from sliceme.util import now

        self.write_channel(
            "w1", [event("w1", 5, now() - 5.0, run_id="node-w1-try-2")]
        )
        brief = self.svc.ready(brief=True)
        # The campaign row's key is the file prefix the writer needs.
        self.assertEqual(brief["branch_key"], "feat--x")
        # The spend verdict of the retry loop: no cap is a plain true.
        self.assertTrue(brief["spend"]["retry"])
        # The brief names the channel once as a dir-relative path: per-node
        # absolute tail paths on a wide phase can cross the host's 4096-byte
        # preview cap, which would stop the loop with an error it cannot
        # act on.
        self.assertEqual(brief["progress_dir"], "feat--x.progress")
        self.assertNotIn("progress_tail", brief["nodes"][0])
        progress_row = brief["nodes"][0]["progress"]
        # The per-node rows carry only the counters a wide phase can afford.
        self.assertEqual(
            sorted(progress_row), ["last_ts", "stalled_ms", "step"]
        )
        self.assertEqual(progress_row["step"], 5)
        self.assertIsNotNone(progress_row["stalled_ms"])
        # The full (non-brief) reply keeps the reader row and the tail path:
        # the interactive engine tool can afford the detail.
        full = self.svc.ready(brief=False)["nodes"][0]
        self.assertEqual(full["progress"]["node"], "w1")
        self.assertIn("attention", full["progress"])
        self.assertIn(
            str(campaign.progress_tail_path(self.root, "feat/x", "w1")),
            full["progress_tail"],
        )
        # A phase-0 brief lists only the ready member; every definition carries
        # the progress fields, populated when the child wrote events.
        self.assertEqual([n["id"] for n in brief["nodes"]], ["w1"])
        self.assertIsNotNone(brief["nodes"][0]["progress"])

    def test_try_counts_follow_the_run_key_suffix(self):
        # The writer rotates the tail by lines: a busy first run can leave no
        # `run_id` of its own while the salvage retake's `-try-2` key
        # survives, so a retained retake key proves at least two tries.
        self.assertEqual(
            progress.try_count([event("w1", 1, 1.0, run_id="node-w1")]), 1
        )
        self.assertEqual(
            progress.try_count(
                [event("w1", 1, 1.0, run_id="node-w1-try-2")]
            ),
            2,
        )
        self.assertEqual(
            progress.try_count(
                [
                    event("w1", 1, 1.0, run_id="node-w1"),
                    event("w1", 2, 2.0, run_id="node-w1-try-2"),
                ]
            ),
            2,
        )
        # A node with no events at all is one try, never zero.
        self.assertEqual(progress.try_count([]), 1)
        self.assertEqual(progress.try_count([event("w1", 1, 1.0, run_id="")]), 1)

    def test_spend_state_guards_the_retry(self):
        row = self.svc.require_campaign()
        # The adapter owns the spend file; the engine reads it in `ready`.
        campaign.write_budget(
            self.root,
            row["work_branch"],
            {"active_seconds": 3000.0, "spent_usd": 0.0},
        )
        self.svc.set_budget(seconds=4000, usd=None, clear=False)
        spend = self.svc.ready(brief=True)["spend"]
        self.assertEqual(spend["wall_seconds_cap"], 4000.0)
        # 3000 active seconds is within 80% of the 4000-second cap.
        self.assertTrue(spend["retry"])
        campaign.write_budget(
            self.root,
            row["work_branch"],
            {"active_seconds": 3400.0, "spent_usd": 0.0},
        )
        spend = self.svc.ready(brief=True)["spend"]
        self.assertFalse(spend["retry"], "a campaign near its cap takes one try")

    def test_dispatch_progress_names_the_campaign(self):
        self.write_channel("w1", [event("w1", 1, 1.0)])
        report = surface.dispatch(self.svc, "progress", {"node": []})
        self.assertEqual(report["campaign"], "feat--x")
        self.assertEqual(report["nodes"][0]["step"], 1)

    def test_a_tail_beyond_the_cap_reads_bounded(self):
        # The writer rotates; the reader still bounds its own read.
        self.progress_dir.mkdir(parents=True, exist_ok=True)
        events = [
            json.dumps(event("w1", index, 100.0 + index)) + "\n"
            for index in range(4000)
        ]
        (self.progress_dir / "w1.jsonl").write_text("".join(events), encoding="utf8")
        parsed = progress.read_tail(
            campaign.progress_tail_path(self.root, "feat/x", "w1")
        )
        self.assertLessEqual(len(parsed), 1200)
        self.assertEqual(parsed[-1]["step"], 3999)


if __name__ == "__main__":
    unittest.main()
