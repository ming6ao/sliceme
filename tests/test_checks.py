"""The synchronous combined-tree check runner and its persistent cache.

The runner replaces the executor queue: it runs one check set over a wave's
combined tree, writes one terminal row to the ``checks`` table, and reuses that
row by fingerprint.  These tests pin the cache (a resumed node reads it), the
``only`` filter, the advisory policy, the wave ``--current`` verbs, and
delivery reading its checks as evidence.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sliceme import campaign, integrate, surface
from sliceme.sandbox import Sandbox, backend_available, coerce_sandbox, resolve_sandbox, wrap_command
from sliceme.service import Service
from sliceme.util import SlicemeError, write_json
from sliceme.verifier import CheckSpec, checks_from_config

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin" / "sliceme"


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


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


class ChecksCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]
    nodes = [{"id": "w1", "owns": ["dir:src/a"], "depends_on": [], "acceptance": ["true"]}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "src" / "a").mkdir(parents=True)
        (self.root / "src" / "a" / "x.py").write_text("a = 1\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        Service.init_plane(self.root, feature_branch="feat/x", checks=self.checks)
        self.svc = Service(self.root)
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "checks",
                "feature_branch": "feat/x",
                "base": "main",
                "nodes": self.nodes,
            },
        )
        self.runner = self.svc.checks()

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def run_check(self, checks, **kwargs):
        return self.runner.run(
            source=kwargs.pop("source", "node:w1"),
            commit=kwargs.pop("commit", "HEAD"),
            checks=checks,
            **kwargs,
        )

    def workspace(self):
        unit = self.svc.create_campaign_workspace()
        return Path(unit["worktree"])


class CheckCacheTests(ChecksCase):
    def test_a_run_records_one_terminal_row_and_caches_it(self):
        first = self.run_check([CheckSpec("ok", "true")])
        self.assertFalse(first["cached"])
        self.assertEqual(first["status"], "passed")
        self.assertTrue(first["fingerprint"])
        self.assertEqual(first["sandbox_digest"], Sandbox().digest())

        second = self.run_check([CheckSpec("ok", "true")])
        self.assertTrue(second["cached"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.svc.store.list_checks()), 1)

    def test_a_terminal_failure_is_cached_not_rerun(self):
        specs = [CheckSpec("ok", "false")]
        first = self.run_check(specs)
        self.assertEqual(first["status"], "failed")
        self.assertNotEqual(first["exit_code"], 0)

        again = self.run_check(specs)
        self.assertTrue(again["cached"])
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["status"], "failed")

    def test_a_different_check_vector_invalidates_the_cache(self):
        one = self.run_check([CheckSpec("ok", "true")])
        two = self.run_check([CheckSpec("ok", "true"), CheckSpec("extra", "true")])
        self.assertFalse(two["cached"])
        self.assertNotEqual(one["fingerprint"], two["fingerprint"])

    def test_only_keeps_one_check(self):
        specs = [CheckSpec("ok", "true"), CheckSpec("no", "false")]
        full = self.run_check(specs)
        one = self.run_check(specs, only=["no"])
        self.assertEqual(json.loads(one["commands"]), ["false"])
        self.assertNotEqual(full["fingerprint"], one["fingerprint"])
        self.assertEqual(one["status"], "failed")

    def test_only_that_matches_no_check_is_rejected(self):
        with self.assertRaises(SlicemeError):
            self.run_check([CheckSpec("ok", "true")], only=["absent"])

    def test_an_advisory_check_does_not_fail_the_run(self):
        row = self.run_check([CheckSpec("lint", "false", required=False)])
        self.assertEqual(row["status"], "passed")
        results = json.loads(row["results"])
        self.assertEqual(results[0]["status"], "failed")
        self.assertFalse(results[0]["required"])

    def test_a_run_needs_a_source_commit_and_check(self):
        with self.assertRaises(SlicemeError):
            self.runner.run(source="", commit="HEAD", checks=[CheckSpec("ok", "true")])
        with self.assertRaises(SlicemeError):
            self.runner.run(source="node:w1", commit=None, checks=[CheckSpec("ok", "true")])
        with self.assertRaises(SlicemeError):
            self.runner.run(source="node:w1", commit="HEAD")


class WaveCheckTests(ChecksCase):
    def record_wave(self):
        worktree = self.workspace()
        (worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        return self.svc.record_wave(0, messages={"w1": "change"})

    def test_check_current_runs_the_recorded_wave(self):
        self.record_wave()
        first = self.svc.check_wave()
        self.assertEqual(first["wave"], 0)
        self.assertEqual(first["members"], ["w1"])
        self.assertEqual(first["status"], "passed")
        self.assertFalse(first["cached"])
        self.assertTrue(first["commit_ref"])

        again = self.svc.check_wave()
        self.assertTrue(again["cached"])
        self.assertEqual(again["id"], first["id"])

    def test_check_current_uses_the_recorded_wave_not_the_next(self):
        # Two waves: w1 owns src/a, w2 owns docs and depends on w1.  After wave
        # 0 is recorded the next wave is wave 1, but the worktree head is wave
        # 0's commit, so check --current must run wave 0's acceptance.
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "checks",
                "feature_branch": "feat/x",
                "base": "feat/x",
                "concurrency": 2,
                "nodes": [
                    {
                        "id": "w1",
                        "owns": ["dir:src/a"],
                        "depends_on": [],
                        "acceptance": ["echo wave-zero"],
                    },
                    {
                        "id": "w2",
                        "owns": ["dir:docs"],
                        "depends_on": ["w1"],
                        "acceptance": ["echo wave-one"],
                    },
                ],
            },
        )
        worktree = self.workspace()
        (worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        self.svc.record_wave(0, messages={"w1": "change"})

        result = self.svc.check_wave()
        self.assertEqual(result["wave"], 0)
        self.assertEqual(result["members"], ["w1"])
        commands = json.loads(result["commands"])
        self.assertIn("echo wave-zero", commands)
        self.assertNotIn("echo wave-one", commands)

    def test_wave_record_current_uses_the_engine_wave_index(self):
        worktree = self.workspace()
        (worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        result = surface.dispatch(
            self.svc,
            "wave",
            {"record": True, "current": True, "messages": '{"w1": "change"}'},
        )
        self.assertEqual(result["wave"], 0)
        self.assertEqual([c["node"] for c in result["candidates"]], ["w1"])

    def test_wave_record_without_wave_or_current_is_rejected(self):
        with self.assertRaises(SlicemeError):
            surface.dispatch(self.svc, "wave", {"record": True})

    def test_check_current_requires_the_current_flag(self):
        with self.assertRaises(SlicemeError):
            surface.dispatch(self.svc, "check", {})

    def test_a_resumed_check_reads_the_cache(self):
        """A second Service (a resumed run) reuses the stored verdict."""
        self.record_wave()
        first = self.svc.check_wave()
        resumed = Service(self.root)
        try:
            again = resumed.check_wave()
            self.assertTrue(again["cached"])
            self.assertEqual(again["id"], first["id"])
        finally:
            resumed.close()

    def test_status_reports_check_counts(self):
        self.record_wave()
        self.svc.check_wave()
        status = self.svc.status()
        self.assertEqual(status["checks"], {"passed": 1})
        self.assertNotIn("executor", status)


class WaveCheckVectorTests(ChecksCase):
    nodes = [
        {"id": "w1", "owns": ["dir:src/a"], "depends_on": [], "acceptance": ["echo node-a", "true"]},
        {"id": "w2", "owns": ["dir:src/b"], "depends_on": [], "acceptance": ["echo node-b", "echo node-a"]},
    ]

    def test_check_wave_unions_plane_and_acceptance_checks(self):
        with mock.patch(
            "sliceme.checks.CheckRunner.run", return_value={"status": "passed"}
        ) as run:
            self.svc.check_wave()
        commands = [spec.command for spec in run.call_args.kwargs["checks"]]
        # Plane checks first, then the wave's acceptance commands, de-duplicated.
        self.assertEqual(commands, ["true", "echo node-a", "echo node-b"])

    def test_check_vector_uses_only_the_recorded_members(self):
        # Wave 0 has two members.  Only w1 records a candidate, because w2
        # changed nothing in its owned directory.  The wave-0 check vector must
        # include w1's acceptance and exclude w2's.
        worktree = self.workspace()
        (worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        self.svc.record_wave(0, messages={"w1": "change"})
        with mock.patch(
            "sliceme.checks.CheckRunner.run", return_value={"status": "passed"}
        ) as run:
            result = self.svc.check_wave()
        self.assertEqual(result["wave"], 0)
        self.assertEqual(result["members"], ["w1"])
        commands = [spec.command for spec in run.call_args.kwargs["checks"]]
        self.assertEqual(commands, ["true", "echo node-a"])
        self.assertNotIn("echo node-b", commands)


class GpuWaveTests(ChecksCase):
    nodes = [
        {
            "id": "w1",
            "owns": ["dir:src/a"],
            "depends_on": [],
            "acceptance": ["true"],
            "gpu": "T2",
        }
    ]

    def test_check_wave_passes_the_wave_gpu_tier(self):
        with mock.patch(
            "sliceme.checks.CheckRunner.run", return_value={"status": "passed"}
        ) as run:
            self.svc.check_wave()
        self.assertEqual(run.call_args.kwargs["gpu"], "T2")


class DeliveryCheckTests(ChecksCase):
    def test_delivery_runs_its_checks_and_reads_the_cache(self):
        worktree = self.workspace()
        (worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        self.svc.record_wave(0, messages={"w1": "change"})
        self.svc.review_decision(action="approve", all_commits=True, actor="test")
        created = {"url": "https://example.test/pull/1", "number": 1}
        with mock.patch.object(integrate.pullrequest, "require"), mock.patch.object(
            integrate.pullrequest, "find", return_value=None
        ), mock.patch.object(
            integrate.pullrequest, "create", return_value=created
        ), mock.patch.object(
            integrate.gitutil, "push"
        ):
            results = integrate.deliver_pull_request(
                self.svc.store,
                self.root,
                self.svc.config,
                campaign=self.svc.campaign_key(),
            )
        self.assertEqual([result.status for result in results], ["landed"])
        self.assertEqual([check.status for check in results[0].checks], ["passed"])

        rows = self.svc.store.list_checks(statuses=["passed"])
        self.assertEqual([row["source"] for row in rows], ["deliver"])
        self.assertIn("[passed] ok: true", rows[0]["output"])

        # The same check set is served from the cache, so delivery never re-runs.
        again = self.runner.run(
            source="deliver",
            commit=rows[0]["commit_ref"],
            checks=checks_from_config(self.svc.config),
        )
        self.assertTrue(again["cached"])
        self.assertEqual(again["id"], rows[0]["id"])


class DeliveryGpuTests(ChecksCase):
    nodes = [
        {
            "id": "w1",
            "owns": ["dir:src/a"],
            "depends_on": [],
            "acceptance": ["true"],
            "gpu": "T1",
        }
    ]

    def test_delivery_passes_the_campaign_gpu_tier(self):
        worktree = self.workspace()
        (worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        self.svc.record_wave(0, messages={"w1": "change"})
        self.svc.review_decision(action="approve", all_commits=True, actor="test")
        with mock.patch(
            "sliceme.checks.CheckRunner.run",
            return_value={"status": "passed", "results": "[]"},
        ) as run, mock.patch.object(
            integrate.pullrequest, "require"
        ), mock.patch.object(
            integrate.pullrequest, "find", return_value=None
        ), mock.patch.object(
            integrate.pullrequest,
            "create",
            return_value={"url": "https://example.test/pull/1", "number": 1},
        ), mock.patch.object(
            integrate.gitutil, "push"
        ):
            integrate.deliver_pull_request(
                self.svc.store,
                self.root,
                self.svc.config,
                campaign=self.svc.campaign_key(),
            )
        self.assertEqual(run.call_args.kwargs["gpu"], "T1")


class FailingDeliveryCase(ChecksCase):
    checks = [{"name": "no", "command": "false", "required": True}]

    def test_a_failing_check_blocks_delivery(self):
        worktree = self.workspace()
        (worktree / "src" / "a" / "x.py").write_text("a = 2\n")
        self.svc.record_wave(0, messages={"w1": "change"})
        self.svc.review_decision(action="approve", all_commits=True, actor="test")
        with mock.patch.object(integrate.pullrequest, "require"):
            results = integrate.deliver_pull_request(
                self.svc.store,
                self.root,
                self.svc.config,
                campaign=self.svc.campaign_key(),
            )
        self.assertEqual([result.status for result in results], ["failed"])
        self.assertIn("checks failed", results[0].detail)


class CliCheckTests(ChecksCase):
    def test_cli_wave_record_current_and_check_current(self):
        worktree = self.workspace()
        (worktree / "src" / "a" / "x.py").write_text("a = 2\n")

        recorded = run_cli(
            [
                "--json",
                "wave",
                "--record",
                "--current",
                "--messages",
                '{"w1": "change"}',
            ],
            self.root,
        )
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual(json.loads(recorded.stdout)["wave"], 0)

        checked = run_cli(["--json", "check", "--current"], self.root)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        payload = json.loads(checked.stdout)
        self.assertEqual(payload["wave"], 0)
        self.assertEqual(payload["status"], "passed")


class SandboxTests(unittest.TestCase):
    def test_default_is_unsandboxed_and_wraps_unchanged(self):
        self.assertEqual(wrap_command("echo hi", Sandbox()), "echo hi")

    def test_resolution_precedence(self):
        config = {"policy": {"sandbox": {"mode": "bwrap", "network": False}}}
        dag = {"sandbox": {"mode": "unshare"}}
        # Explicit override wins, then the DAG, then the plane policy.
        self.assertEqual(resolve_sandbox(dag, config, override="none").mode, "none")
        self.assertEqual(resolve_sandbox(dag, config).mode, "unshare")
        self.assertEqual(resolve_sandbox(None, config).mode, "bwrap")
        self.assertFalse(resolve_sandbox(None, config).network)
        self.assertEqual(resolve_sandbox(None, None).mode, "none")

    def test_coerce_rejects_unknown_mode(self):
        with self.assertRaises(SlicemeError):
            coerce_sandbox({"mode": "seccomp"})

    def test_digest_tracks_isolation_semantics(self):
        self.assertNotEqual(Sandbox().digest(), Sandbox(mode="bwrap").digest())
        self.assertNotEqual(
            Sandbox(mode="bwrap", network=True).digest(),
            Sandbox(mode="bwrap", network=False).digest(),
        )

    @unittest.skipUnless(backend_available("bwrap"), "bwrap not installed")
    def test_bwrap_wrap_is_well_formed(self):
        wrapped = wrap_command(
            "echo hi", Sandbox(mode="bwrap", network=False), worktree="/tmp/wt"
        )
        self.assertTrue(wrapped.startswith("bwrap "))
        self.assertIn("--unshare-net", wrapped)
        self.assertIn("--chdir /tmp/wt", wrapped)
        self.assertTrue(wrapped.endswith("-- /bin/sh -lc 'echo hi'"))


if __name__ == "__main__":
    unittest.main()
