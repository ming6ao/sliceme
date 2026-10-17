"""Smoke tests for the CLI adapter.

It is generated from :mod:`sliceme.surface`; these tests pin the shared
action surface and the end-to-end flow.
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
from sliceme import campaign, surface  # noqa: E402
from sliceme.service import Service  # noqa: E402
from sliceme.util import write_json  # noqa: E402


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

            out = run_cli(["--json", "start", "--design", "DESIGN.md", "--check", "ok=true"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue((root / ".sliceme" / "config.json").is_file())

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

            # First call from the main checkout initialises the plane and
            # records the campaign; the worktree is created later by `open`.
            out = run_cli(["--json", "start", "--design", "DESIGN.md"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            first = json.loads(out.stdout)
            self.assertTrue(first["initialized"])
            self.assertEqual(first["work_branch"], "feat/design")
            self.assertIsNone(first["worktree"])
            self.assertTrue((root / ".sliceme" / "config.json").is_file())

            # A repeated call from the same checkout is idempotent.
            out = run_cli(["--json", "start"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            again = json.loads(out.stdout)
            self.assertFalse(again["initialized"])
            self.assertEqual(again["campaign"], first["campaign"])

            # `init` remains a hidden alias for `start`.
            out = run_cli(["--json", "init"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["campaign"], first["campaign"])

    def test_status_health_and_simulate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            run_cli(["--json", "start", "--design", "DESIGN.md"], root)

            out = run_cli(["--json", "doctor"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue(json.loads(out.stdout)["ok"])

            out = run_cli(["--json", "simulate", "--no-checks"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("phases", json.loads(out.stdout))

    def test_start_ignores_state_subdirectories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)

            out = run_cli(["--json", "start", "--design", "DESIGN.md"], root)
            self.assertEqual(out.returncode, 0, out.stderr)

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

    def test_cwd_native_start_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            run_cli(["--json", "start", "--feature-branch", "feat/x"], root)

            # `status --short` prints the bound campaign's key from any checkout.
            out = run_cli(["status", "--short"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), "feat--x")

    def test_campaign_phase_record_approve_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)

            # Campaign bootstrap: `start` records the campaign branch; the
            # worktree is created later from the delivery base.
            remote_tmp = tempfile.TemporaryDirectory()
            self.addCleanup(remote_tmp.cleanup)
            remote = Path(remote_tmp.name) / "origin.git"
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=tmp, check=True)
            out = run_cli(
                ["--json", "start", "--feature-branch", "feat/x", "--check", "ok=true"],
                root,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIsNone(json.loads(out.stdout)["worktree"])

            # A plain `status` reports the feature branch and the one writer.
            out = run_cli(["status", "--json"], root)
            status = json.loads(out.stdout)
            self.assertEqual(status["work_branch"], "feat/x")
            self.assertEqual([u["branch"] for u in status["units"]], ["feat/x"])

            write_json(
                campaign.dag_path(root, "feat/x"),
                {
                    "campaign": "cli",
                    "feature_branch": "feat/x",
                    "base": "main",
                    "nodes": [
                        {"id": "w1", "label": "test", "owns": ["."], "depends_on": []}
                    ],
                },
            )
            out = run_cli(["--json", "open"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            worktree = Path(json.loads(out.stdout)["unit"]["worktree"])
            (worktree / "a.txt").write_text("w1\n")
            out = run_cli(
                ["--json", "record"],
                root,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            record = json.loads(out.stdout)["records"][0]

            out = run_cli(
                ["--json", "approve"],
                root,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            out = run_cli(["--json", "deliver"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            delivered = json.loads(out.stdout)
            self.assertEqual([r["status"] for r in delivered["results"]], ["delivered"])
            pr = delivered["pull_request"]
            self.assertTrue(pr and pr["url"])
            # The campaign branch is pushed; the delivery base stays as it was
            # until a human merges the pull request.
            self.assertEqual(
                subprocess.run(
                    ["git", "show", f"{delivered['source']}:a.txt"],
                    cwd=tmp,
                    capture_output=True,
                    text=True,
                ).stdout,
                "w1\n",
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "show", "main:a.txt"], cwd=tmp, capture_output=True, text=True
                ).stdout,
                "hi\n",
            )

            # Re-running deliver returns the same pull request, not a new one.
            out = run_cli(["--json", "deliver"], root)
            rerun = json.loads(out.stdout)
            self.assertEqual(rerun["pull_request"]["url"], pr["url"])

            out = run_cli(["--json", "report", "--narrative", "delivered"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            report = json.loads(out.stdout)
            self.assertEqual(Path(report["path"]).name, "feat--x.report.md")
            self.assertIn("delivered", report["content"])

    def test_cli_status_normalizes_same_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            out = run_cli(["--json", "start", "--feature-branch", "feat/x", "--check", "ok=true"], root)
            self.assertEqual(out.returncode, 0, out.stderr)

            nodes = [
                {"id": "a", "owns": ["src/x"], "depends_on": []},
                {"id": "b", "owns": ["src/x"], "depends_on": ["a"]},
            ]
            write_json(
                campaign.dag_path(root, "feat/x"),
                {"campaign": "cli", "feature_branch": "feat/x", "base": "main", "nodes": nodes},
            )

            out = run_cli(["--json", "status"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            status = json.loads(out.stdout)
            self.assertEqual(status["dag_merge"]["merged"], {"b": "a"})
            self.assertEqual([w["members"] for w in status["dag_phases"]], [["a"]])
            on_disk = campaign.load_dag(root, "feat/x")
            self.assertEqual([n["id"] for n in on_disk["nodes"]], ["a"])

    def test_cli_resume_and_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            run_cli(["--json", "start", "--feature-branch", "feat/x", "--check", "ok=true"], root)

            state_dir = root / ".sliceme"
            (state_dir / "feat--x.dag.json").write_text(
                json.dumps(
                    {
                        "campaign": "cli",
                        "feature_branch": "feat/x",
                        "nodes": [{"id": "w1", "owns": ["src"]}],
                    }
                )
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

            out = run_cli(["--json", "resume"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            plan = json.loads(out.stdout)
            self.assertEqual(plan["work_branch"], "feat/x")
            self.assertIn("w1", plan["nodes"])

            out = run_cli(["--json", "sessions"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            entries = json.loads(out.stdout)["sessions"]
            self.assertEqual(entries[0]["work_branch"], "feat/x")
            self.assertEqual(entries[0]["session_file"], "/tmp/s1.jsonl")


    def test_cli_runs_two_campaigns_by_flag(self):
        """`--campaign` scopes a call; a bare `status` reports the plane."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)

            out = run_cli(
                ["start", "--feature-branch", "feat/x",
                 "--check", "ok=true"],
                root,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            out = run_cli(
                ["start", "--feature-branch", "feat/y"],
                root,
            )
            self.assertEqual(out.returncode, 0, out.stderr)

            out = run_cli(["--json", "status", "--campaign", "feat/x"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["target_branch"], "feat/x")

            out = run_cli(["--json", "status", "--campaign", "feat/y"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["target_branch"], "feat/y")

            out = run_cli(["--json", "status"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            summary = json.loads(out.stdout)
            self.assertTrue(summary.get("plane"))
            self.assertEqual(
                {c["target_branch"] for c in summary["campaigns"]},
                {"feat/x", "feat/y"},
            )

    def test_status_default_is_dense_and_verbose_is_nested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            out = run_cli(["--json", "start", "--feature-branch", "feat/x", "--check", "ok=true"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            write_json(
                campaign.dag_path(root, "feat/x"),
                {
                    "campaign": "cli",
                    "feature_branch": "feat/x",
                    "base": "main",
                    "nodes": [
                        {"id": "w1", "owns": ["src/a"], "depends_on": []},
                        {"id": "w2", "owns": ["src/b"], "depends_on": ["w1"]},
                    ],
                },
            )
            # w1 has a record, so the DAG projection marks phase 0 done.
            svc = Service(root)
            svc.store.create_record(
                head_commit="0" * 40, summary=None, node="w1", campaign="feat--x"
            )
            svc.store.conn.commit()
            svc.close()

            # The default human output is the dense summary, not the nested dump.
            out = run_cli(["status"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("campaign:    cli", out.stdout)
            self.assertIn("  w0 w1 recorded", out.stdout)
            self.assertIn("  w1 w2 pending", out.stdout)
            self.assertIn("phase 1 [pending]: w2", out.stdout)
            self.assertNotIn("campaign_key:", out.stdout)

            # --verbose restores the full nested dump.
            out = run_cli(["status", "--verbose"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("campaign_key:", out.stdout)
            self.assertNotIn("  w0 w1 recorded", out.stdout)

            # --json stays the nested dump and carries no lines key.
            out = run_cli(["status", "--json"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertNotIn("lines", json.loads(out.stdout))

            # --verbose --json is still the nested dump, with no lines key.
            out = run_cli(["status", "--verbose", "--json"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("campaign_key", json.loads(out.stdout))
            self.assertNotIn("lines", json.loads(out.stdout))

            # --dense --json emits the summary and its lines.
            out = run_cli(["status", "--dense", "--json"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            lines = json.loads(out.stdout)["lines"]
            self.assertEqual(lines[0], "campaign:    cli")
            self.assertIn("  w0 w1 recorded", lines)
            self.assertIn("  w1 w2 pending", lines)
            self.assertIn("phase 1 [pending]: w2", lines)

            # --short still prints only the campaign name; it also wins over
            # --dense (both are explicit status views).
            out = run_cli(["status", "--short"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), "feat--x")
            out = run_cli(["status", "--dense", "--short"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), "feat--x")

    def test_status_plane_human_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            first = run_cli(
                [
                    "--json",
                    "start",
                    "--feature-branch",
                    "feat/x",
                                        "--check",
                    "ok=true",
                ],
                root,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            second = run_cli(
                [
                    "--json",
                    "init",
                    "--feature-branch",
                    "feat/y",
                                        "--base",
                    "main",
                ],
                root,
            )
            self.assertEqual(second.returncode, 0, second.stderr)

            # Two campaigns bind no single one, so the default human output is
            # the compact plane list, not a dense single-campaign summary.
            out = run_cli(["status"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.splitlines()[0], "campaigns: 2")
            self.assertIn("feat--x [working]", out.stdout)
            self.assertIn("feat--y [working]", out.stdout)

    def test_secondary_verbs_keep_the_human_dump(self):
        # doctor/gc/simulate/sessions/resume are their own verbs; the CLI must
        # print each verb's own human dump, never the dense summary's lines.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            out = run_cli(["--json", "start", "--feature-branch", "feat/x", "--check", "ok=true"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            write_json(
                campaign.dag_path(root, "feat/x"),
                {
                    "campaign": "cli",
                    "feature_branch": "feat/x",
                    "base": "main",
                    "nodes": [{"id": "w1", "owns": ["src"], "depends_on": []}],
                },
            )
            write_json(
                campaign.session_path(root, "feat/x"),
                {"feature_branch": "feat/x", "nodes": {"w1": {"status": "running"}}},
            )

            cases = [
                (["doctor"], "checks:"),
                (["gc"], "removed_worktrees:"),
                (["simulate", "--no-checks"], "record_count:"),
                (["sessions"], "sessions:"),
                (["resume"], "resume_plan:"),
            ]
            for argv, marker in cases:
                out = run_cli(argv, root)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertIn(marker, out.stdout)
                # The dense summary's header line is the tell that the CLI took
                # the lines branch instead of each mode's own dump.
                self.assertNotIn("phase size:", out.stdout)

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


if __name__ == "__main__":
    unittest.main()
