"""Time and tool metrics: the ``progress`` projection (docs/observability.md).

These tests pin the durable half of the observability design:

* the per-attempt tool time and thinking time roll up per agent role;
* the tool rollup carries the verifier's tool calls and the executor check time
  stays out of it;
* the queue wait and the verification time come from the ``jobs`` table;
* a stale heartbeat marks a running node stalled.
"""

import tempfile
import unittest
from pathlib import Path

from sliceme import campaign
from sliceme.service import Service
from sliceme.util import write_json


class ProgressCase(unittest.TestCase):
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
        _git(self.root, "checkout", "-q", "-b", "feat/x")
        Service.init_plane(self.root, checks=self.checks)
        self.svc = Service(self.root)
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "metrics",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": 2,
                "nodes": self.nodes,
            },
        )

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def finish(self, *, node, agent, tool_seconds, tool_durations, duration, **over):
        self.svc.begin_attempt(node=node, unit="campaign", attempt=1, agent=agent)
        return self.svc.end_attempt(
            node=node,
            attempt=1,
            status="ok",
            duration=duration,
            tool_seconds=tool_seconds,
            tool_durations=tool_durations,
            **over,
        )

    def test_time_splits_by_tool_and_agent(self):
        self.finish(
            node="w1",
            agent="worker",
            tool_seconds=60.0,
            tool_durations={"bash": 45.0, "read": 15.0},
            slowest_commands=[
                {"command": "cargo", "tool": "bash", "seconds": 45.0, "calls": 3}
            ],
            duration=100.0,
            turns=10,
            tool_calls=20,
            tools={"bash": 3, "read": 5},
            tokens_in=100,
            tokens_out=20,
            cost=0.1,
        )
        # A verifier's tool calls are real tool calls, so they fold into the
        # rollup, tagged with the role.
        self.finish(
            node="w1",
            agent="verifier",
            tool_seconds=20.0,
            tool_durations={"read": 20.0},
            duration=30.0,
            turns=4,
            tool_calls=8,
            tools={"read": 4},
        )

        view = self.svc.progress()
        self.assertEqual(view["totals"]["tool_seconds"], 80.0)
        self.assertEqual(view["totals"]["thinking_seconds"], 50.0)
        self.assertEqual(view["totals"]["tool_calls"], 28)
        self.assertEqual(view["totals"]["turns"], 14)
        self.assertEqual(view["by_agent"]["worker"]["tool_seconds"], 60.0)
        self.assertEqual(view["by_agent"]["worker"]["thinking_seconds"], 40.0)
        self.assertEqual(view["by_agent"]["verifier"]["tool_seconds"], 20.0)
        self.assertEqual(view["by_agent"]["verifier"]["thinking_seconds"], 10.0)

        tools = {item["tool"]: item for item in view["tools"]}
        self.assertEqual(tools["bash"]["seconds"], 45.0)
        self.assertEqual(tools["bash"]["by_agent"]["worker"], 45.0)
        self.assertEqual(tools["read"]["seconds"], 35.0)
        self.assertEqual(tools["read"]["by_agent"]["verifier"], 20.0)
        self.assertEqual(tools["read"]["by_agent"]["worker"], 15.0)
        self.assertEqual(tools["bash"]["calls"], 3)
        self.assertEqual(tools["read"]["calls"], 9)

        commands = {item["command"]: item for item in view["commands"]}
        self.assertEqual(commands["cargo"]["seconds"], 45.0)
        self.assertEqual(commands["cargo"]["calls"], 3)

        node = next(item for item in view["nodes"] if item["id"] == "w1")
        self.assertEqual(node["wave"], 0)
        self.assertEqual(node["tool_seconds"], 20.0)
        self.assertEqual(node["thinking_seconds"], 10.0)

    def test_queue_wait_and_verification_stay_out_of_the_tool_rollup(self):
        job_id = self.svc.store.create_job(
            source="node:w1",
            commit_ref="deadbeef",
            commands=["true"],
            campaign=self.svc.campaign_key(),
        )
        self.svc.store.conn.execute(
            "UPDATE jobs SET status='passed', requested_at=1000.0, started_at=1002.5,"
            " finished_at=1010.0, duration=7.5 WHERE id=?",
            (job_id,),
        )
        self.svc.store.conn.commit()

        view = self.svc.progress()
        self.assertEqual(view["verification"]["jobs"], 1)
        self.assertEqual(view["verification"]["passed"], 1)
        self.assertEqual(view["verification"]["pass_rate"], 1.0)
        self.assertEqual(view["verification"]["executor_seconds"], 7.5)
        self.assertEqual(view["totals"]["queue_wait_seconds"], 2.5)
        # The executor check time never enters the tool rollup.
        self.assertEqual(view["tools"], [])

    def test_a_stale_heartbeat_is_stalled(self):
        write_json(
            campaign.state_path(self.root, "feat/x"),
            {"campaign": "metrics", "nodes": {"w1": {"status": "running"}}},
        )
        write_json(
            campaign.heartbeat_path(self.root, "feat/x", "w1"),
            {"node": "w1", "updated_at": 1.0},
        )
        view = self.svc.progress()
        node = next(item for item in view["nodes"] if item["id"] == "w1")
        self.assertTrue(node["stalled"])
        self.assertIsNotNone(node["heartbeat_age"])
        self.assertEqual(view["totals"]["running"], 1)

    def test_node_filter_narrows_the_view(self):
        write_json(
            campaign.state_path(self.root, "feat/x"),
            {"campaign": "metrics", "nodes": {"w1": {"status": "done"}, "w2": {"status": "pending"}}},
        )
        view = self.svc.progress(node="w1")
        self.assertEqual([item["id"] for item in view["nodes"]], ["w1"])
        self.assertEqual(view["totals"]["nodes"], 1)


def _git(root: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    )


if __name__ == "__main__":
    unittest.main()
