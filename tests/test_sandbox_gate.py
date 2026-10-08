"""Project sandbox manifests and the coordinator's sandbox gate.

Phase 2 pins: manifest discovery/validation, fail-closed gating when a campaign
requires a sandbox, GPU runner requirements, digest pinning (a manifest change
invalidates cached verdicts), setup-before-acceptance, and command wrapping.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sliceme import sandbox as sandbox_mod
from sliceme.sandbox import (
    Sandbox,
    discover_sandbox,
    resolve_sandbox,
    wrap_command,
)
from sliceme.service import Service
from sliceme.util import SlicemeError, config_path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin" / "sliceme"
SANDBOX_SCRIPT = "#!/bin/sh\nexec \"$@\"\n"


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


class SandboxGateCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "tools").mkdir()
        (self.root / "tools" / "sandbox.sh").write_text(SANDBOX_SCRIPT)
        (self.root / "a.txt").write_text("hi\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        Service.init_plane(self.root, feature_branch="feat/x", checks=self.checks)
        self.svc = Service(self.root)
        self.runner = self.svc.checks()

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    # -- helpers --------------------------------------------------------
    def write_manifest(self, data, *, commit=True):
        path = self.root / "sliceme.sandbox.json"
        path.write_text(json.dumps(data))
        if commit:
            run("git", "add", "-A", cwd=self.root)
            run("git", "commit", "-qm", "sandbox manifest", cwd=self.root)
        return path

    def set_policy(self, **policy):
        cfg = json.loads(config_path(self.root).read_text())
        cfg["policy"] = {**cfg.get("policy", {}), **policy}
        config_path(self.root).write_text(json.dumps(cfg))
        self.runner = self.svc.checks()
        return cfg

    def manifest(self, **overrides):
        data = {"version": 1, "command": ["sh", "tools/sandbox.sh"]}
        data.update(overrides)
        return data

    def run_checks(self, commands, **kwargs):
        return self.runner.run(
            source=kwargs.pop("source", "node:w1"),
            commit=kwargs.pop("commit", "HEAD"),
            commands=commands,
            **kwargs,
        )


class DiscoveryTests(SandboxGateCase):
    def test_manifest_is_discovered_and_resolved(self):
        self.write_manifest(self.manifest(network=False, setup=["true"]))
        discovered = discover_sandbox(self.root)
        self.assertIsNotNone(discovered)
        self.assertEqual(discovered.manifest, "sliceme.sandbox.json")
        self.assertFalse(discovered.network)
        self.assertEqual(discovered.setup, ("true",))
        self.assertTrue(discovered.configured)

    def test_policy_overrides_the_manifest(self):
        self.write_manifest(self.manifest())
        sandbox = resolve_sandbox(
            None, {"policy": {"sandbox": "bwrap"}}, root=self.root
        )
        self.assertEqual(sandbox.mode, "bwrap")
        self.assertIsNone(sandbox.manifest)

    def test_dag_pointer_loads_the_manifest(self):
        manifest = self.manifest()
        self.write_manifest(manifest)
        resolved = resolve_sandbox(
            {"sandbox": {"path": "sliceme.sandbox.json"}}, {}, root=self.root
        )
        self.assertEqual(resolved.command, ("sh", "tools/sandbox.sh"))

    def test_dag_digest_mismatch_is_rejected(self):
        self.write_manifest(self.manifest())
        with self.assertRaises(SlicemeError) as ctx:
            resolve_sandbox(
                {"sandbox": {"path": "sliceme.sandbox.json", "digest": "0" * 64}},
                {},
                root=self.root,
            )
        self.assertIn("digest mismatch", str(ctx.exception))

    def test_unknown_manifest_version_is_rejected(self):
        self.write_manifest(self.manifest(version=99))
        with self.assertRaises(SlicemeError) as ctx:
            discover_sandbox(self.root)
        self.assertIn("version", str(ctx.exception))

    def test_malformed_manifest_is_reported_not_crashed(self):
        (self.root / "sliceme.sandbox.json").write_text("{ not json")
        info = self.svc.sandbox_info()
        self.assertFalse(info["ok"])
        # `status` must still render, with the sandbox flagged.
        status = self.svc.status()
        self.assertFalse(status["sandbox"]["ok"])


class GateTests(SandboxGateCase):
    def test_missing_manifest_fails_closed_when_required(self):
        self.set_policy(require_sandbox=True)
        info = self.svc.sandbox_info()
        self.assertFalse(info["ok"])
        self.assertIn("sandbox not configured", info["error"])
        with self.assertRaises(SlicemeError):
            self.run_checks(["true"])

    def test_gate_passes_when_a_manifest_exists(self):
        self.write_manifest(self.manifest())
        self.set_policy(require_sandbox=True)
        info = self.svc.sandbox_info()
        self.assertTrue(info["ok"], info)
        self.assertEqual(info["manifest"], "sliceme.sandbox.json")

    def test_bad_command_fails_validation(self):
        self.write_manifest(self.manifest(command=["tools/does-not-exist.sh"]))
        self.set_policy(require_sandbox=True)
        info = self.svc.sandbox_info()
        self.assertFalse(info["ok"])
        self.assertIn("not found", info["error"])

    def test_gpu_job_requires_a_runner(self):
        self.write_manifest(self.manifest())
        with mock.patch.object(sandbox_mod, "default_gpu_runner", return_value=None):
            with self.assertRaises(SlicemeError) as ctx:
                self.runner.require_sandbox(gpu_required=True)
        self.assertIn("GPU", str(ctx.exception))

    def test_gpu_command_in_manifest_satisfies_the_gate(self):
        self.write_manifest(
            self.manifest(gpu={"command": ["sh", "tools/sandbox.sh", "--tier", "{tier}"]})
        )
        with mock.patch.object(sandbox_mod, "default_gpu_runner", return_value=None):
            profile = self.runner.require_sandbox(gpu_required=True)
        self.assertEqual(profile.gpu_command[0], "sh")


class ExecutionTests(SandboxGateCase):
    def test_setup_runs_once_before_acceptance(self):
        self.write_manifest(self.manifest(setup=["touch setup-marker"]))
        row = self.run_checks(["test -f setup-marker"])
        self.assertEqual(row["status"], "passed", row["output"])
        names = [line for line in row["output"].splitlines() if line.startswith("[")]
        self.assertTrue(any("setup[0]" in line for line in names), row["output"])

    def test_manifest_change_invalidates_the_cached_fingerprint(self):
        self.write_manifest(self.manifest(setup=["true"]))
        first = self.run_checks(["true"])
        self.assertTrue(self.run_checks(["true"])["cached"])

        # Tightening the manifest (different setup) must not reuse the verdict.
        self.write_manifest(self.manifest(setup=["true", "true"]))
        changed = self.run_checks(["true"])
        self.assertFalse(changed["cached"])
        self.assertNotEqual(first["fingerprint"], changed["fingerprint"])

    def test_failing_setup_blocks_acceptance(self):
        self.write_manifest(self.manifest(setup=["false"]))
        row = self.run_checks(["true"])
        self.assertEqual(row["status"], "failed")
        self.assertIn("setup[0]", row["output"])


class WrappingTests(unittest.TestCase):
    def test_custom_command_wraps_with_sh_lc(self):
        sandbox = Sandbox(command=("sh", "tools/run.sh"))
        self.assertEqual(
            wrap_command("pytest -q", sandbox),
            "sh tools/run.sh /bin/sh -lc 'pytest -q'",
        )

    def test_gpu_runner_wraps_outside_the_sandbox(self):
        sandbox = Sandbox(
            command=("sh", "s.sh"),
            gpu_command=("mygpu", "--tier", "{tier}", "--"),
        )
        wrapped = wrap_command("python t.py", sandbox, tier="T1")
        self.assertTrue(wrapped.startswith("mygpu --tier T1 -- "))
        self.assertIn("sh s.sh /bin/sh -lc", wrapped)
        self.assertIn("python t.py", wrapped)

    def test_none_is_unchanged(self):
        self.assertEqual(wrap_command("echo hi", Sandbox()), "echo hi")


class CliGateTests(SandboxGateCase):
    def test_status_reports_the_sandbox_gate(self):
        self.write_manifest(self.manifest())
        out = run_cli(["--json", "status"], self.root)
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)["sandbox"]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["manifest"], "sliceme.sandbox.json")
        self.assertTrue(payload["digest"])

    def test_status_reports_a_failing_gate_when_required(self):
        self.set_policy(require_sandbox=True)
        out = run_cli(["--json", "status"], self.root)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("sandbox not configured", out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
