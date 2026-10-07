"""Executor queue, sandboxed runs, and fingerprint invalidation.

Phase 1 pins the engine-side executor: one serialized runner, a sandbox
profile folded into the fingerprint, dedupe of terminal jobs, a commit batch,
an ``only`` command filter, cancellation, and crash-lease recovery.  Phase 3
adds that delivery's trusted checks run as executor jobs.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sliceme import campaign, integrate
from sliceme.sandbox import (
    Sandbox,
    backend_available,
    coerce_sandbox,
    resolve_sandbox,
    wrap_command,
)
from sliceme.service import Service
from sliceme.util import SlicemeError, write_json

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


class ExecutorCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "a.txt").write_text("hi\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        Service.init_plane(self.root, checks=self.checks)
        self.svc = Service(self.root)
        self.executor = self.svc.executor()

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()


class QueueTests(ExecutorCase):
    def submit(self, commands, **kwargs):
        return self.executor.submit(
            source=kwargs.pop("source", "node:w1"),
            commit=kwargs.pop("commit", "HEAD"),
            commands=commands,
            **kwargs,
        )

    def test_submit_run_passes_and_records_fingerprint(self):
        result = self.submit(["true"])
        job = result["job"]
        self.assertFalse(result["cached"])
        self.assertEqual(job["status"], "queued")
        self.assertTrue(job["fingerprint"])
        self.assertEqual(job["sandbox_digest"], Sandbox().digest())

        done = self.executor.drain()
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["status"], "passed")
        self.assertEqual(done[0]["exit_code"], 0)
        self.assertIn("[passed] acceptance[0]: true", done[0]["output"])

    def test_a_passing_job_is_deduped_not_rerun(self):
        first = self.submit(["true"])
        self.assertEqual(first["job"]["status"], "queued")
        executed = self.executor.drain()
        self.assertEqual(executed[0]["status"], "passed")

        second = self.submit(["true"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["job"]["id"], first["job"]["id"])

        # Draining again does not re-run the cached job.
        self.assertEqual(self.executor.drain(), [])

    def test_failing_command_marks_the_job_failed(self):
        self.submit(["false"])
        done = self.executor.drain()
        self.assertEqual(done[0]["status"], "failed")
        self.assertNotEqual(done[0]["exit_code"], 0)

    def test_priority_orders_the_queue(self):
        low = self.submit(["true"], priority=0)
        high = self.submit(["true"], priority=5)
        first = self.executor.run_next()
        self.assertEqual(first["id"], high["job"]["id"])
        # The low-priority job remains queued.
        self.assertEqual(self.executor.store.get_job(low["job"]["id"])["status"], "queued")

    def test_timeout_is_persisted_and_fingerprinted(self):
        short_job = self.submit(["true"], timeout=10)["job"]
        long_job = self.submit(["true"], timeout=20)["job"]
        self.assertEqual(short_job["timeout"], 10)
        self.assertNotEqual(short_job["fingerprint"], long_job["fingerprint"])

    def test_cancel_only_cancels_queued_jobs(self):
        job = self.submit(["true"])
        cancelled = self.executor.cancel(job["job"]["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.executor.drain(), [])

    def test_recover_orphaned_running_job(self):
        job = self.submit(["true"])["job"]
        claimed = self.executor.store.claim_next_job(runner_pid=os.getpid())
        self.assertEqual(claimed["id"], job["id"])
        # Age the lease so recovery treats it as orphaned.
        self.executor.store.update_job(job["id"], started_at=0.0)
        self.executor.store.conn.commit()
        self.assertEqual(self.executor.recover_orphans(lease=1.0), 1)
        self.assertEqual(self.executor.store.get_job(job["id"])["status"], "queued")

    def test_submit_requires_source_commit_and_commands(self):
        with self.assertRaises(SlicemeError):
            self.executor.submit(source="", commit="HEAD", commands=["true"])
        with self.assertRaises(SlicemeError):
            self.executor.submit(source="node:w1", commit=None, commands=["true"])
        with self.assertRaises(SlicemeError):
            self.executor.submit(source="node:w1", commit="HEAD", commands=[])


class BatchAndCacheTests(ExecutorCase):
    def add_commit(self, name):
        (self.root / name).write_text(name + "\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", name, cwd=self.root)
        return run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip()

    def test_a_batch_of_commits_is_verified_by_one_drain(self):
        first = run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip()
        second = self.add_commit("second.txt")
        result = self.executor.submit(
            source="wave:0", commits=[first, second], commands=["true"]
        )
        self.assertFalse(result["cached"])
        self.assertEqual(len(result["jobs"]), 2)
        self.assertEqual([job["status"] for job in result["jobs"]], ["queued", "queued"])

        # One drain verifies the whole batch.
        done = self.executor.drain()
        self.assertEqual(len(done), 2)
        self.assertTrue(all(job["status"] == "passed" for job in done))
        self.assertEqual({job["commit_ref"] for job in done}, {first, second})

    def test_a_batch_may_be_passed_as_the_commit_argument(self):
        first = run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip()
        second = self.add_commit("second.txt")
        result = self.executor.submit(
            source="wave:0", commit=[first, second], commands=["true"]
        )
        self.assertEqual(len(result["jobs"]), 2)

    def test_a_cached_batch_reports_cached(self):
        first = run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip()
        second = self.add_commit("second.txt")
        jobs = self.executor.submit(
            source="wave:0", commits=[first, second], commands=["true"]
        )
        self.executor.drain()
        again = self.executor.submit(
            source="wave:0", commits=[first, second], commands=["true"]
        )
        self.assertTrue(again["cached"])
        self.assertEqual(
            [job["id"] for job in again["jobs"]], [job["id"] for job in jobs["jobs"]]
        )
        self.assertEqual(self.executor.drain(), [])

    def test_a_terminal_failure_is_cached_not_rerun(self):
        first = self.executor.submit(source="node:w1", commit="HEAD", commands=["false"])
        done = self.executor.drain()
        self.assertEqual(done[0]["status"], "failed")

        # DEC-3: a failed job stands as a verdict and is not re-run.
        again = self.executor.submit(source="node:w1", commit="HEAD", commands=["false"])
        self.assertTrue(again["cached"])
        self.assertEqual(again["job"]["id"], first["job"]["id"])
        self.assertEqual(again["job"]["status"], "failed")
        self.assertEqual(self.executor.drain(), [])

    def test_only_filter_runs_one_command(self):
        full = self.executor.submit(
            source="node:w1", commit="HEAD", commands=["true", "false"]
        )
        one = self.executor.submit(
            source="node:w1", commit="HEAD", commands=["true", "false"], only=["false"]
        )
        self.assertEqual(json.loads(one["job"]["commands"]), ["false"])
        self.assertNotEqual(full["job"]["fingerprint"], one["job"]["fingerprint"])

        self.executor.cancel(full["job"]["id"])
        done = self.executor.drain()
        self.assertEqual([job["status"] for job in done], ["failed"])

    def test_only_that_matches_no_command_is_rejected(self):
        with self.assertRaises(SlicemeError):
            self.executor.submit(
                source="node:w1", commit="HEAD", commands=["true"], only=["absent"]
            )

    def test_a_check_spec_vector_is_persisted_and_honored(self):
        from sliceme.verifier import CheckSpec

        advisory = [CheckSpec(name="lint", command="false", required=False)]
        result = self.executor.submit(
            source="deliver", commit="HEAD", checks=advisory
        )
        stored = json.loads(result["job"]["checks"])
        self.assertEqual(stored[0]["required"], False)
        done = self.executor.drain()
        # An advisory failure is not a job failure.
        self.assertEqual(done[0]["status"], "passed")
        results = json.loads(done[0]["results"])
        self.assertEqual(results[0]["status"], "failed")
        self.assertFalse(results[0]["required"])


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


class FingerprintSandboxTests(ExecutorCase):
    def test_sandbox_digest_changes_the_fingerprint(self):
        plain = self.executor.submit(
            source="node:w1", commit="HEAD", commands=["true"], sandbox="none"
        )["job"]
        strict = self.executor.submit(
            source="node:w1", commit="HEAD", commands=["true"], sandbox="bwrap"
        )["job"]
        self.assertNotEqual(plain["fingerprint"], strict["fingerprint"])
        self.assertNotEqual(plain["sandbox_digest"], strict["sandbox_digest"])


class DeliveryCase(unittest.TestCase):
    """Shared fixture for delivery: the trusted checks run as executor jobs."""

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
        run("git", "checkout", "-q", "-b", "feat/x", cwd=self.root)
        Service.init_plane(self.root, base="feat/x", checks=self.checks)
        self.svc = Service(self.root)
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "deliver",
                "feature_branch": "feat/x",
                "base": "feat/x",
                "nodes": self.nodes,
            },
        )

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()


class DeliveryRoutingTests(DeliveryCase):
    def test_delivery_runs_its_checks_as_an_executor_job(self):
        unit = self.svc.create_wave_workspace(0, base="feat/x")
        (Path(unit["worktree"]) / "src" / "a" / "x.py").write_text("a = 2\n")
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
        self.assertEqual(
            [check.status for check in results[0].checks], ["passed"]
        )
        self.assertEqual([check.required for check in results[0].checks], [True])
        self.assertEqual(
            [check["status"] for check in results[0].to_dict()["checks"]], ["passed"]
        )
        jobs = self.svc.store.list_jobs(statuses=["passed"])
        self.assertEqual([job["source"] for job in jobs], ["deliver"])
        self.assertIn("[passed] ok: true", jobs[0]["output"])


class AdvisoryDeliveryTests(DeliveryCase):
    """A delivery check with ``required: false`` stays advisory."""

    checks = [{"name": "lint", "command": "false", "required": False}]

    def test_a_failing_advisory_check_does_not_block_delivery(self):
        unit = self.svc.create_wave_workspace(0, base="feat/x")
        (Path(unit["worktree"]) / "src" / "a" / "x.py").write_text("a = 2\n")
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
        # The plane's `required: false` is honored through the executor job.
        self.assertEqual([result.status for result in results], ["landed"])
        self.assertEqual([check.status for check in results[0].checks], ["failed"])
        self.assertFalse(results[0].checks[0].required)


class CliExecTests(ExecutorCase):
    def test_cli_exec_submit_run_wait(self):
        submitted = run_cli(
            [
                "--json",
                "exec",
                "--submit",
                "--source",
                "node:w1",
                "--commit",
                "HEAD",
                "--command",
                "true",
            ],
            self.root,
        )
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        job = json.loads(submitted.stdout)["job"]
        self.assertEqual(job["status"], "queued")

        drained = run_cli(["--json", "exec", "--run"], self.root)
        self.assertEqual(drained.returncode, 0, drained.stderr)
        self.assertEqual(json.loads(drained.stdout)["executed"], 1)

        waited = run_cli(["--json", "exec", "--wait", "--job", str(job["id"])], self.root)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(json.loads(waited.stdout)["job"]["status"], "passed")

        status = run_cli(["--json", "exec"], self.root)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["counts"]["passed"], 1)


if __name__ == "__main__":
    unittest.main()
