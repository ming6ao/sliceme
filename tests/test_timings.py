"""Timing statistics from the pi-subagents child transcripts.

Pi-subagents writes one transcript for each child run beside the pi session
file.  The engine reads a thinking step from each assistant message and a tool
step from each tool start/end pair.  These tests pin the parsing, the campaign
scope, the de-duplication, and both reports.
"""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign, cli, timings
from sliceme.service import Service
from sliceme.util import write_json


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


def record(kind, run_id, agent, cwd, ts, **fields):
    data = {
        "version": 1,
        "recordType": kind,
        "runId": run_id,
        "agent": agent,
        "childIndex": 0,
        "cwd": str(cwd),
        "ts": ts,
        "timestamp": "",
    }
    data.update(fields)
    return data


def assistant(run_id, agent, cwd, ts, duration_ms):
    return record(
        "message",
        run_id,
        agent,
        cwd,
        ts,
        role="assistant",
        sourceEventType="message_end",
        message={
            "role": "assistant",
            "durationMs": duration_ms,
            "content": [{"type": "thinking", "thinking": "plan"}],
        },
    )


def tool_start(run_id, agent, cwd, ts, call_id, name, payload):
    return record(
        "tool_start",
        run_id,
        agent,
        cwd,
        ts,
        toolCallId=call_id,
        toolName=name,
        argsPayload=json.dumps(payload),
    )


def tool_end(run_id, agent, cwd, ts, call_id, name, error=False):
    return record(
        "tool_end",
        run_id,
        agent,
        cwd,
        ts,
        toolCallId=call_id,
        toolName=name,
        isError=error,
    )


#: A realistic epoch in milliseconds, so the parser keeps the value.
BASE = 1_790_000_000_000


def write_transcript(path, rows):
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )


class TimingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "a.txt").write_text("hi\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        Service.init_plane(self.root, feature_branch="feat/x", checks=[])
        self.svc = Service(self.root)
        self.worktree = Path(self.svc.create_campaign_workspace()["worktree"])
        self.session_dir = Path(self.tmp.name) / "sessions" / "project"
        self.artifacts = self.session_dir / "subagent-artifacts"
        self.artifacts.mkdir(parents=True)
        self.session_file = self.session_dir / "2026-01-01T00-00-00Z_s.jsonl"
        self.session_file.write_text("")
        write_json(
            timings.pi_session_path(self.root, "feat/x"),
            {
                "campaign": "feat/x",
                "pi": {
                    "session_id": "session-1",
                    "session_file": str(self.session_file),
                    "cwd": str(self.root),
                },
            },
        )

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def add_run(self, run_id, agent, cwd, rows, timestamp=None):
        meta = {
            "runId": run_id,
            "parentSessionId": "session-1",
            "agent": agent,
            "timestamp": timestamp if timestamp is not None else BASE,
            "transcriptPath": str(self.artifacts / f"{run_id}_{agent}_transcript.jsonl"),
        }
        write_json(self.artifacts / f"{run_id}_{agent}_meta.json", meta)
        write_transcript(Path(meta["transcriptPath"]), rows)

    @staticmethod
    def tool_pair(run_id, agent, cwd, begin, seconds, call_id, name, payload=None):
        """One tool start/end pair, in milliseconds from *begin*."""
        return [
            tool_start(run_id, agent, cwd, begin, call_id, name, payload or {}),
            tool_end(
                run_id,
                agent,
                cwd,
                begin + int(seconds * 1000),
                call_id,
                name,
            ),
        ]

    def add_campaign_runs(self):
        cwd = self.worktree
        worker = "sliceme-worker"
        rows = [assistant("run-w", worker, cwd, BASE + 5_000, 5_000)]
        rows += self.tool_pair(
            "run-w", worker, cwd, BASE + 6_000, 130, "c1", "bash",
            {"command": "sleep 130"},
        )
        rows += self.tool_pair(
            "run-w", worker, cwd, BASE + 137_000, 90, "c2", "bash",
            {"command": "sleep 90"},
        )
        rows += self.tool_pair(
            "run-w", worker, cwd, BASE + 228_000, 1, "c3", "read",
            {"path": "a.txt"},
        )
        rows.append(assistant("run-w", worker, cwd, BASE + 230_000, 2_500))
        self.add_run("run-w", worker, cwd, rows)

        planner = "sliceme-planner"
        rows = [assistant("run-p", planner, self.root, BASE + 2_000, 3_000)]
        rows += self.tool_pair(
            "run-p", planner, self.root, BASE + 3_000, 5, "p1", "bash",
            {"command": "true"},
        )
        self.add_run(
            "run-p",
            planner,
            self.root,
            rows,
            timestamp=int(self.svc.campaign["created_at"] * 1000) + 10_000,
        )

        # A run outside the worktree belongs to another session's work.
        elsewhere = Path(self.tmp.name) / "elsewhere"
        self.add_run(
            "run-other",
            worker,
            elsewhere,
            [assistant("run-other", worker, elsewhere, BASE, 9_000)],
        )


class ParseTests(unittest.TestCase):
    def test_an_unpaired_tool_call_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_transcript.jsonl"
            write_transcript(
                path,
                [
                    assistant("r", "sliceme-worker", "/w", 1_000_000, 1_000),
                    tool_start(
                        "r", "sliceme-worker", "/w", 1_001_000, "c1",
                        "bash", {"command": "ls"},
                    ),
                ],
            )
            parsed = timings.parse_transcript(path)
        self.assertEqual(len(parsed["steps"]), 1)
        self.assertEqual(parsed["steps"][0]["kind"], "thinking")

    def test_a_malformed_line_does_not_stop_the_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_transcript.jsonl"
            path.write_text(
                "not json\n"
                + json.dumps(assistant("r", "sliceme-worker", "/w", 1_000_000, 2_000))
                + "\n",
                encoding="utf-8",
            )
            parsed = timings.parse_transcript(path)
        self.assertEqual(len(parsed["steps"]), 1)


class IngestTests(TimingCase):
    def test_ingest_scopes_runs_and_is_idempotent(self):
        self.add_campaign_runs()
        first = self.svc.ingest_timings()
        self.assertEqual(first["runs"], 2)
        self.assertEqual(first["thinking"], 3)
        self.assertEqual(first["tools"], 4)
        self.assertEqual(first["inserted"], 7)
        second = self.svc.ingest_timings()
        self.assertEqual(second["inserted"], 0)
        rows = self.svc.store.list_timings(campaign=self.svc.campaign_key())
        self.assertEqual(len(rows), 7)
        self.assertTrue(all(row["source"] for row in rows))

    def test_a_long_command_keeps_the_command_text(self):
        self.add_campaign_runs()
        self.svc.ingest_timings()
        rows = self.svc.store.list_timings(campaign=self.svc.campaign_key())
        commands = {
            row["command"]: row["duration"] for row in rows if row["command"]
        }
        self.assertEqual(commands["sleep 130"], 130.0)
        self.assertEqual(commands["sleep 90"], 90.0)

    def test_report_writes_the_dedicated_timing_file(self):
        self.add_campaign_runs()
        result = self.svc.report(narrative="done", slow_seconds=120)
        timing_file = Path(result["timing"]["path"])
        self.assertTrue(timing_file.is_file())
        self.assertEqual(timing_file.name, "feat--x.timings.md")
        content = timing_file.read_text(encoding="utf-8")
        self.assertIn("## Thinking steps", content)
        self.assertIn("## Tool calls", content)
        self.assertIn("## Longest commands", content)
        self.assertIn("sleep 130", content)
        self.assertIn("sliceme-planner", content)
        # The fast command keeps a preview, and the report says it is not slow.
        self.assertIn("1 command(s) ran for at least 2.0m", content)
        # The main report carries the short section.
        self.assertIn("## Timing", result["content"])
        self.assertIn("The complete timing report is", result["content"])

    def test_the_threshold_selects_the_slow_commands(self):
        self.add_campaign_runs()
        default = self.svc.report()
        self.assertEqual(len(default["timing"]["summary"]["slow_commands"]), 1)
        lower = self.svc.report(slow_seconds=60)
        self.assertEqual(len(lower["timing"]["summary"]["slow_commands"]), 2)

    def test_the_cli_prints_the_timing_report(self):
        self.add_campaign_runs()
        self.svc.report()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--root", str(self.root), "report"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("Timing report", text)
        self.assertIn("Longest commands", text)

    def test_record_ingests_the_worker_steps(self):
        self.add_campaign_runs()
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "demo",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": 1,
                "nodes": [
                    {"id": "n1", "dir": ".", "depends_on": [], "tasks": ["change"]}
                ],
            },
        )
        (self.worktree / "a.txt").write_text("changed\n")
        self.svc.record_phase(0, messages={"n1": "change"})
        rows = self.svc.store.list_timings(campaign=self.svc.campaign_key())
        self.assertGreaterEqual(len(rows), 7)

    def test_no_session_binding_is_a_no_op(self):
        timings.pi_session_path(self.root, "feat/x").unlink()
        result = self.svc.ingest_timings()
        self.assertEqual(result["inserted"], 0)


if __name__ == "__main__":
    unittest.main()
