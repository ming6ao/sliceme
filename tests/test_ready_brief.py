"""The bounded `ready` reply (`ready --brief`).

The workflow host returns at most 4096 bytes of standard output, one detailed
node definition can exceed that, and the campaign loop therefore reads the brief
form. These tests pin three things:

* the full form still carries `goal` and `tasks`;
* the brief form leaves them out and names the plan file in `dag`;
* a brief reply stays under the host cap for a plan whose full reply does not.

The payload floor is measured, not guessed: the long task text below crosses
4096 bytes in the full form with three nodes.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin" / "sliceme"
FAKE_GH_BIN = Path(__file__).resolve().parent / "bin"

sys.path.insert(0, str(REPO_ROOT))
from sliceme import campaign  # noqa: E402
from sliceme.service import Service  # noqa: E402
from sliceme.util import write_json  # noqa: E402

#: The pi-subagents preview cap for one host stream.
HOST_PREVIEW_BYTES = 4096

LONG_TASK = (
    "Rewrite the parser stage so that each token carries its span, then update "
    "every call site, add round-trip tests, and document the new error message "
    "in the reference."
)


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


def run_cli(args, cwd):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PATH"] = str(FAKE_GH_BIN) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [sys.executable, str(BIN), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )


class ReadyBriefCase(unittest.TestCase):
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
                "campaign": "brief",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": concurrency,
                "nodes": nodes,
            },
        )

    def detailed_dag(self, count=12):
        """A plan whose phase-0 nodes carry long tasks, as a careful plan does."""
        nodes = []
        for index in range(count):
            tasks = 9 if index == 0 else (6 if index == 1 else 2)
            nodes.append(
                {
                    "id": f"n{index}",
                    "label": f"node {index}",
                    "owns": [f"src/{'abc'[index % 3]}/n{index}"],
                    "depends_on": [],
                    "goal": f"Goal for node {index}. {LONG_TASK}",
                    "tasks": [f"{LONG_TASK} ({index}.{task})" for task in range(tasks)],
                    "acceptance": ["true"],
                }
            )
        self.dag(nodes)

    def payload_bytes(self, data):
        return len(json.dumps(data, indent=2, sort_keys=True, default=str).encode())

    def test_the_full_reply_carries_the_node_text(self):
        self.dag(
            [
                {
                    "id": "w1",
                    "owns": ["src/a"],
                    "depends_on": [],
                    "goal": "Add the endpoint.",
                    "tasks": ["Write the handler.", "Add the test."],
                    "acceptance": ["true"],
                }
            ]
        )
        node = self.svc.ready()["nodes"][0]
        self.assertEqual(node["goal"], "Add the endpoint.")
        self.assertEqual(node["tasks"], ["Write the handler.", "Add the test."])
        self.assertEqual(node["dir"], "src/a")

    def test_the_brief_reply_omits_the_node_text_and_names_the_plan(self):
        self.dag(
            [
                {
                    "id": "w1",
                    "owns": ["src/a"],
                    "depends_on": [],
                    "goal": "Add the endpoint.",
                    "tasks": ["Write the handler."],
                    "acceptance": ["true"],
                }
            ]
        )
        reply = self.svc.ready(brief=True)
        self.assertEqual(reply["ready"], ["w1"])
        node = reply["nodes"][0]
        self.assertEqual(node["id"], "w1")
        self.assertEqual(node["dir"], "src/a")
        self.assertEqual(node["label"], None)
        self.assertNotIn("goal", node)
        self.assertNotIn("tasks", node)
        plan = Path(reply["dag"])
        self.assertTrue(plan.is_file(), reply["dag"])
        self.assertEqual(plan, campaign.dag_path(self.root, "feat/x"))
        stored = json.loads(plan.read_text())
        self.assertEqual([item["id"] for item in stored["nodes"]], ["w1"])
        self.assertEqual(stored["nodes"][0]["goal"], "Add the endpoint.")

    def test_the_brief_reply_stays_under_the_host_cap(self):
        self.detailed_dag()
        full = self.svc.ready()
        brief = self.svc.ready(brief=True)
        # The defect needs a payload larger than the cap; this plan supplies one.
        self.assertGreater(self.payload_bytes(full), HOST_PREVIEW_BYTES)
        self.assertLess(self.payload_bytes(brief), HOST_PREVIEW_BYTES)
        self.assertEqual(full["ready"], brief["ready"])
        self.assertEqual([node["id"] for node in full["nodes"]], [node["id"] for node in brief["nodes"]])
        self.assertEqual(full["dag"], brief["dag"])

    def test_the_cli_brief_flag_returns_the_brief_reply(self):
        self.detailed_dag()
        out = run_cli(["--json", "ready", "--brief"], self.root)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertLess(len(out.stdout.encode()), HOST_PREVIEW_BYTES)
        brief = json.loads(out.stdout)
        self.assertEqual(brief["phase"], 0)
        self.assertEqual(len(brief["nodes"]), 3)
        for node in brief["nodes"]:
            self.assertNotIn("goal", node)
            self.assertNotIn("tasks", node)
            self.assertIn("dir", node)
        self.assertTrue(Path(brief["dag"]).is_file())

        full = run_cli(["--json", "ready"], self.root)
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertGreater(len(full.stdout.encode()), HOST_PREVIEW_BYTES)
        self.assertIn("goal", json.loads(full.stdout)["nodes"][0])

    def test_a_missing_campaign_reports_a_null_plan(self):
        empty = Service(self.root.parent)
        try:
            reply = empty.ready(brief=True)
        finally:
            empty.close()
        self.assertIsNone(reply["dag"])
        self.assertEqual(reply["nodes"], [])

    def test_the_brief_form_still_reports_the_pause(self):
        self.detailed_dag()
        control = campaign.control_path(self.root, "feat/x")
        write_json(control, {"pause": True, "reason": "budget", "metric": "wall-clock"})
        reply = self.svc.ready(brief=True)
        self.assertEqual(reply["paused"], True)
        self.assertEqual(reply["pause_reason"], "budget")
        self.assertEqual(reply["pause_metric"], "wall-clock")
        self.assertLess(self.payload_bytes(reply), HOST_PREVIEW_BYTES)


if __name__ == "__main__":
    unittest.main()
