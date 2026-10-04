"""Smoke tests for the CLI adapter.

It is generated from :mod:`sliceme.surface`; these tests pin the shared
action surface and the end-to-end flow.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin" / "sliceme"

sys.path.insert(0, str(REPO_ROOT))
from sliceme import campaign, surface  # noqa: E402
from sliceme.util import write_json  # noqa: E402


def run_cli(args, cwd):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(BIN), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )


class CliTests(unittest.TestCase):
    def test_init_and_status_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)

            out = run_cli(["--json", "start", "--check", "ok=true"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue((root / ".sliceme" / "config.json").is_file())

            out = run_cli(["--json", "start", "--name", "alpha"], root)
            self.assertEqual(out.returncode, 0, out.stderr)

            out = run_cli(["status", "--json"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("units", json.loads(out.stdout))

    def test_init_bootstraps_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)

            # First call from the main checkout initialises the plane and a unit.
            out = run_cli(["--json", "start"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            first = json.loads(out.stdout)
            self.assertTrue(first["initialized"])
            self.assertTrue(first["created"])
            self.assertTrue((root / ".sliceme" / "config.json").is_file())
            worktree = Path(first["worktree"])
            self.assertTrue(worktree.is_dir())

            # Second call from inside the unit worktree is a no-op.
            out = run_cli(["--json", "start"], worktree)
            self.assertEqual(out.returncode, 0, out.stderr)
            again = json.loads(out.stdout)
            self.assertFalse(again["initialized"])
            self.assertFalse(again["created"])
            self.assertEqual(again["unit"], first["unit"])

            # A fresh call from the main checkout gets a distinct unit name.
            out = run_cli(["--json", "start"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            third = json.loads(out.stdout)
            self.assertFalse(third["initialized"])
            self.assertTrue(third["created"])
            self.assertNotEqual(third["unit"], first["unit"])

            # `init` remains a hidden alias for `start`.
            out = run_cli(["--json", "init"], worktree)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["unit"], first["unit"])

    def test_status_health_and_simulate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            run_cli(["--json", "start"], root)
            run_cli(["--json", "start", "--name", "alpha"], root)
            run_cli(["--json", "start", "--name", "beta"], root)

            out = run_cli(["--json", "status", "--health"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue(json.loads(out.stdout)["ok"])

            out = run_cli(["--json", "status", "--simulate", "--no-checks"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("waves", json.loads(out.stdout))

    def test_start_ignores_state_subdirectories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)

            out = run_cli(["--json", "start"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            worktree = Path(json.loads(out.stdout)["worktree"])

            # Nothing start created may show up as untracked/modified.
            status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=tmp, capture_output=True, text=True, check=True
            )
            self.assertEqual(status.stdout.strip(), "")

            # Every state subdirectory is covered by the ignore rule.
            for rel in ("config.json", "state.db", "worktrees", "scratch"):
                ignored = subprocess.run(
                    ["git", "check-ignore", f".sliceme/{rel}"],
                    cwd=tmp,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(ignored.returncode, 0, f".sliceme/{rel} not ignored")

            # The unit worktree itself stays clean too.
            wt_status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(wt_status.stdout.strip(), "")

    def test_cwd_native_start_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp, check=True)
            run_cli(["--json", "start", "--no-unit"], root)
            out = run_cli(["--json", "start", "--name", "alpha", "--base", "feat/x"], root)
            worktree = Path(json.loads(out.stdout)["worktree"])

            out = run_cli(["status", "--short"], worktree)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), "alpha")

    def test_campaign_wave_record_approve_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)

            # Campaign bootstrap: check out the feature branch first; `start`
            # records it as the target and never creates one.
            subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp, check=True)
            out = run_cli(
                ["--json", "start", "--no-unit", "--check", "ok=true"],
                root,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIsNone(json.loads(out.stdout)["unit"])

            # A plain `status` has no units and reports the feature branch.
            out = run_cli(["status", "--json"], root)
            status = json.loads(out.stdout)
            self.assertEqual(status["feature_branch"], "feat/x")
            self.assertEqual(status["units"], [])

            write_json(
                campaign.dag_path(root, "feat/x"),
                {
                    "campaign": "cli",
                    "feature_branch": "feat/x",
                    "base": "main",
                    "nodes": [
                        {"id": "w1", "owns": ["dir:."], "depends_on": []}
                    ],
                },
            )
            out = run_cli(["--json", "wave", "--open"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            worktree = Path(json.loads(out.stdout)["unit"]["worktree"])
            (worktree / "a.txt").write_text("w1\n")
            out = run_cli(
                ["--json", "wave", "--record", "--wave", "0", "--messages", '{"w1": "test"}'],
                root,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            candidate = json.loads(out.stdout)["candidates"][0]

            out = run_cli(
                ["--json", "review", "--decision", "approve", "--commit", candidate["head_commit"]],
                root,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            out = run_cli(["--json", "deliver"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            results = json.loads(out.stdout)["results"]
            self.assertEqual([r["status"] for r in results], ["landed"])
            self.assertEqual(
                subprocess.run(
                    ["git", "show", "feat/x:a.txt"], cwd=tmp, capture_output=True, text=True
                ).stdout,
                "w1\n",
            )

            # Re-running deliver is an idempotent no-op.
            out = run_cli(["--json", "deliver"], root)
            rerun = json.loads(out.stdout)["results"]
            self.assertEqual([r["status"] for r in rerun], ["landed"])
            self.assertTrue(rerun[0]["already_up_to_date"])

            out = run_cli(["--json", "review", "--report", "--narrative", "landed"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            report = json.loads(out.stdout)
            self.assertEqual(Path(report["path"]).name, "feat--x.report.md")
            self.assertIn("landed", report["content"])

    def test_cli_status_normalizes_same_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp, check=True)
            out = run_cli(["--json", "start", "--no-unit", "--check", "ok=true"], root)
            self.assertEqual(out.returncode, 0, out.stderr)

            nodes = [
                {"id": "a", "owns": ["dir:src/x"], "depends_on": []},
                {"id": "b", "owns": ["dir:src/x"], "depends_on": ["a"]},
            ]
            write_json(
                campaign.dag_path(root, "feat/x"),
                {"campaign": "cli", "feature_branch": "feat/x", "base": "main", "nodes": nodes},
            )

            out = run_cli(["--json", "status"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            status = json.loads(out.stdout)
            self.assertEqual(status["dag_merge"]["merged"], {"b": "a"})
            self.assertEqual([w["members"] for w in status["dag_waves"]], [["a"]])
            on_disk = campaign.load_dag(root, "feat/x")
            self.assertEqual([n["id"] for n in on_disk["nodes"]], ["a"])

    def test_cli_resume_sessions_and_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp, check=True)
            run_cli(["--json", "start", "--no-unit", "--check", "ok=true"], root)

            state_dir = root / ".sliceme"
            (state_dir / "feat--x.dag.json").write_text(
                json.dumps(
                    {
                        "campaign": "cli",
                        "feature_branch": "feat/x",
                        "nodes": [{"id": "w1", "owns": ["dir:src"]}],
                    }
                )
            )
            (state_dir / "feat--x.state.json").write_text(
                json.dumps({"nodes": {"w1": {"status": "running", "attempts": 1}}})
            )
            (state_dir / "feat--x.session.json").write_text(
                json.dumps(
                    {
                        "campaign": "cli",
                        "feature_branch": "feat/x",
                        "pi": {"session_id": "s1", "session_file": "/tmp/s1.jsonl"},
                        "label": "cli",
                        "status": "suspended",
                        "reason": "user",
                        "suspended_at": 1.0,
                    }
                )
            )

            out = run_cli(["--json", "status", "--resume", "--plan-only"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            plan = json.loads(out.stdout)
            self.assertEqual(plan["feature_branch"], "feat/x")
            self.assertIn("w1", plan["nodes"])

            out = run_cli(["--json", "status", "--sessions"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            entries = json.loads(out.stdout)["sessions"]
            self.assertEqual(entries[0]["feature_branch"], "feat/x")
            self.assertEqual(entries[0]["session_file"], "/tmp/s1.jsonl")

            out = run_cli(["--json", "attempt", "--begin", "--node", "w1"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["status"], "running")
            out = run_cli(
                [
                    "--json",
                    "attempt",
                    "--end",
                    "--node",
                    "w1",
                    "--attempt",
                    "1",
                    "--status",
                    "ok",
                    "--turns",
                    "3",
                ],
                root,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["turns"], 3)

    def test_cli_surface_matches_registry(self):
        """The CLI subcommands are exactly the registry (plus aliases)."""
        import argparse

        from sliceme.cli import build_parser

        sub = next(
            a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)
        )
        expected = {a.name for a in surface.ACTIONS}
        expected |= {alias for a in surface.ACTIONS for alias in a.aliases}
        self.assertEqual(set(sub.choices), expected)

    def test_cli_and_agent_surfaces_share_actions(self):
        """The pi extension's action list must match surface.ACTIONS."""
        ext = REPO_ROOT / "integrations" / "pi" / "unit.ts"
        text = ext.read_text(encoding="utf-8")
        match = re.search(r"SLICEME_ACTIONS\s*=\s*\[(.*?)\]\s*as const", text, re.DOTALL)
        self.assertIsNotNone(match, "SLICEME_ACTIONS not found in pi extension")
        names = re.findall(r'"([a-z_]+)"', match.group(1))
        self.assertEqual(names, [a.name for a in surface.ACTIONS])


if __name__ == "__main__":
    unittest.main()
