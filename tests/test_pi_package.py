"""Guards the pi package contract.

Sliceme supports exactly one install path: ``pi install`` of this package, which
registers one extension exposing the ``sliceme`` engine tool, the Sliceme agent
definitions, and the ``sliceme.campaign`` workflow resource. There is no
separate skill and no second (``sliceme-unit``) tool. These tests fail if that
structure regresses.
"""

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
WORKFLOW = REPO_ROOT / "docs" / "workflow.md"
PACKAGE = REPO_ROOT / "package.json"
PI_DIR = REPO_ROOT / "integrations" / "pi"
AGENTS_DIR = PI_DIR / "agents"
PI_COORDINATOR = PI_DIR / "coordinator.ts"
PI_COMMON = PI_DIR / "common.ts"
PI_RESOURCE = PI_DIR / "campaign-resource.ts"
PI_UNIT = PI_DIR / "unit.ts"

sys.path.insert(0, str(REPO_ROOT))
from sliceme import surface  # noqa: E402


def _node_strip_supported(node: str) -> bool:
    """Whether this Node build can strip TypeScript types at run time (22.6+)."""
    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True
    ).stdout.strip()
    match = re.match(r"v(\d+)\.(\d+)", version)
    return bool(match) and (int(match.group(1)), int(match.group(2))) >= (22, 6)


class PiPackageTests(unittest.TestCase):
    def test_workflow_doc_exists(self):
        # The workflow lives in docs/workflow.md (human docs); the in-session
        # guidance is the tools' promptGuidelines.
        self.assertTrue(WORKFLOW.is_file(), "docs/workflow.md is required")
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("# Sliceme workflow", text)

    def test_bundled_cli_and_package_exist(self):
        self.assertTrue((REPO_ROOT / "bin" / "sliceme").is_file())
        self.assertTrue((REPO_ROOT / "sliceme" / "cli.py").is_file())

    def test_no_alternate_install_path(self):
        # pi is the only supported harness; the package installs the extension,
        # so there is no separate `npx skills add` path to document.
        docs = (README, WORKFLOW, REPO_ROOT / "docs" / "guide.md", REPO_ROOT / "docs" / "reference.md")
        for path in docs:
            self.assertNotIn("npx skills add", path.read_text(encoding="utf-8"))

    def test_no_dead_bootstrap_env(self):
        # `SLICEME_AUTO_BOOTSTRAP` belonged to a removed auto-bootstrap path and
        # should not reappear.
        docs = (README, WORKFLOW, REPO_ROOT / "docs" / "guide.md", REPO_ROOT / "docs" / "reference.md")
        for path in docs:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("SLICEME_AUTO_BOOTSTRAP", text, path)

    def test_sliceme_is_extension_only(self):
        # No skill: the extension owns the `/sliceme` command and the engine tool.
        manifest = json.loads(PACKAGE.read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("pi", {}).get("skills", []), [])
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        self.assertIn('pi.registerCommand("sliceme"', coordinator)
        self.assertIn("sendUserMessage", coordinator)
        # The command activates the engine tool for the session.
        self.assertIn("setActiveTools", coordinator)
        # The old skill-trigger glue is gone.
        self.assertNotIn('pi.on("input"', coordinator)
        self.assertNotIn("/skill:sliceme", coordinator)
        self.assertNotIn("SKILL_INVOCATION", coordinator)
        self.assertNotIn("hasDesignDocument", coordinator)

    def test_engine_tool_registers_inactive(self):
        # A plain session does not advertise Sliceme; `/sliceme` turns the tool
        # on via `pi.setActiveTools`.
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        self.assertIn("defaultActive: false", coordinator)
        self.assertNotIn("defaultActive: true", coordinator)

    def test_pi_extension_is_a_thin_forwarder(self):
        # There is no single-agent bootstrap: the extension registers the engine
        # tool, the agents, and the resource through the shared helpers.
        text = PI_COORDINATOR.read_text(encoding="utf-8")
        self.assertIn('from "./common.ts"', text)
        self.assertIn('from "./campaign-resource.ts"', text)
        self.assertIn("runSliceme(pi, ctx", text)
        self.assertNotIn("bindingIsStale", text)
        self.assertNotIn("bootstrap(ctx)", text)
        self.assertNotIn("before_agent_start", text)

    def test_pi_package_manifest(self):
        self.assertTrue(PACKAGE.is_file(), "package.json is required for `pi install`")
        manifest = json.loads(PACKAGE.read_text(encoding="utf-8"))
        self.assertIn("pi-package", manifest.get("keywords", []))
        extensions = manifest.get("pi", {}).get("extensions", [])
        self.assertEqual(extensions, ["./integrations/pi/coordinator.ts"])
        for shipped in (
            "integrations/pi/common.ts",
            "integrations/pi/campaign-resource.ts",
            "integrations/pi/agents/",
        ):
            self.assertIn(shipped, manifest.get("files", []))
        self.assertTrue(PI_COMMON.is_file())
        self.assertTrue(PI_RESOURCE.is_file())

    def test_no_second_unit_tool_or_agent(self):
        # The `sliceme-unit` tool, the addressing agent, and the verifier agent
        # are retired. The loop uses the builtin `reviewer`.
        self.assertFalse(PI_UNIT.exists(), "unit.ts is retired")
        self.assertFalse((AGENTS_DIR / "addressing.md").exists())
        self.assertFalse((AGENTS_DIR / "verifier.md").exists())
        manifest = json.loads(PACKAGE.read_text(encoding="utf-8"))
        self.assertNotIn("./integrations/pi/unit.ts", manifest.get("files", []))

    def test_engine_tool_forwards_verbs(self):
        # The tool forwards the engine verbs and their flags; it does not own a
        # scheduler.
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        for verb in ("start", "status", "ready", "plan", "wave", "check", "review", "deliver"):
            self.assertIn(f'"{verb}"', coordinator, verb)
        self.assertIn("ENGINE_ACTIONS", coordinator)
        self.assertIn("ACTION_FLAGS", coordinator)
        self.assertIn('name: "sliceme"', coordinator)
        self.assertIn("function engineArgs(", coordinator)

    def test_campaign_resource_is_registered(self):
        # The trusted resource is registered in `session_start` and disposed in
        # `session_shutdown`.
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        resource = PI_RESOURCE.read_text(encoding="utf-8")
        self.assertIn('const CAMPAIGN_RESOURCE = "sliceme.campaign"', resource)
        self.assertIn("registerWorkflowResource", coordinator)
        self.assertIn("loadPiSubagents(\"workflow-resources\")", coordinator)
        self.assertIn("hostCommands", resource)
        self.assertIn('resolve: (args', coordinator)
        # The six fixed host commands, built from literals.
        for verb in (
            '["status"]',
            '["ready"]',
            '["wave", "--record", "--current"]',
            '["check", "--current"]',
            '["review", "--decision", "approve"]',
            '["deliver"]',
        ):
            self.assertIn(verb, resource, verb)
        # Registration is disposed on shutdown.
        self.assertIn("resourceRegistration?.dispose()", coordinator)
        self.assertIn('pi.on("session_shutdown"', coordinator)

    def test_resource_validates_bounded_fields(self):
        # `resolve` validates every field and rejects the rest. The real unit
        # test is `tests/campaign_resource_test.mjs`; these source checks keep the
        # contract visible.
        resource = PI_RESOURCE.read_text(encoding="utf-8")
        self.assertIn("CAMPAIGN_TOKEN", resource)
        self.assertIn("CAMPAIGN_RESOURCE_FIELDS", resource)
        self.assertIn('new Set(["campaign", "waveCap", "nodeCap"])', resource)
        for bound in ("1 to 64", "1 to 256"):
            self.assertIn(bound, resource, bound)
        self.assertIn("unsupported fields", resource)
        # The engine path comes from the captured closure, never a relative path.
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        self.assertIn("captureEngine()", coordinator)
        self.assertIn("resolveSlicemeInvocation()", coordinator)
        self.assertIn("path.resolve", coordinator)
        self.assertIn("resolveCampaignResource(args, engine)", coordinator)

    def test_sliceme_agents_are_registered(self):
        # The planner and worker register through the installed pi-subagents
        # owner; the verifier and addressing agents are gone.
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        self.assertIn('"pi-subagents:runtime-agent-register:v1"', coordinator)
        self.assertIn('"sliceme-planner"', coordinator)
        self.assertIn('"sliceme-worker"', coordinator)
        self.assertIn("registerAgentViaEvents", coordinator)
        self.assertIn("readAgentDefinition(", coordinator)
        self.assertIn("agentRegistrations", coordinator)
        planner = (AGENTS_DIR / "planner.md").read_text(encoding="utf-8")
        worker = (AGENTS_DIR / "worker.md").read_text(encoding="utf-8")
        self.assertIn("name: planner", planner)
        self.assertIn("name: worker", worker)
        self.assertNotIn("sliceme-unit", worker)

    def test_orchestration_duplication_is_deleted(self):
        # The TypeScript orchestration code and the readiness/wave duplication
        # are gone; pi-subagents owns child execution.
        source = PI_COORDINATOR.read_text(encoding="utf-8") + PI_COMMON.read_text(encoding="utf-8")
        for gone in (
            "runSubagent",
            "runTracked",
            "spawnNodes",
            "runWorker",
            "verifyNodes",
            "readyNodes",
            "normalizeDir",
            "ownsOverlap",
            "ensureWaves",
            "refreshWaves",
            "reconcileWaves",
            "dagFingerprint",
            "spawnReviewServer",
            "addressingBatches",
            "CampaignStateStore",
        ):
            self.assertNotIn(gone, source, gone)

    def test_retired_actions_are_gone(self):
        # The executor queue verbs, attempts, progress, review comments, and the
        # browser server are retired; the engine exposes one ready verb and one
        # campaign review verb.
        names = {a.name for a in surface.ACTIONS}
        for gone in (
            "submit",
            "verify",
            "handoff",
            "declare",
            "integrate",
            "commit",
            "exec",
            "attempt",
            "progress",
        ):
            self.assertNotIn(gone, names)
        for present in ("deliver", "review", "wave", "check", "ready", "status"):
            self.assertIn(present, names)
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        for gone in ("spawn", "record", "verify", "exec", "progress"):
            self.assertNotIn(f'case "{gone}"', coordinator)

    def test_campaign_actions_are_documented(self):
        names = {a.name for a in surface.ACTIONS}
        self.assertIn("deliver", names)
        self.assertIn("wave", names)
        self.assertIn("review", names)
        self.assertIn("check", names)
        # The workflow doc documents the campaign additions.
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("`deliver`", workflow)
        self.assertIn("`wave`", workflow)
        self.assertIn("`review`", workflow)
        self.assertIn("--no-unit", workflow)

    def test_session_suspend_resume_contract(self):
        # The extension writes the descriptor, sets and clears the pause flag,
        # hard-aborts the in-flight turn to suspend, and resumes on the
        # `session_start` hook. It never shadows pi's `/resume`.
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        common = PI_COMMON.read_text(encoding="utf-8")

        # Descriptor / pause helpers live in common.ts.
        for needle in (
            "export function sessionPath",
            "export function controlPath",
            "export function activeCampaignPath",
        ):
            self.assertIn(needle, common)

        # Commands and lifecycle hooks.
        self.assertIn('pi.registerCommand("suspend"', coordinator)
        self.assertIn('pi.registerCommand("campaigns"', coordinator)
        self.assertIn('pi.on("session_start"', coordinator)
        self.assertIn('pi.on("session_shutdown"', coordinator)
        self.assertNotIn('pi.registerCommand("resume"', coordinator)
        self.assertIn("writeSessionDescriptor", coordinator)
        self.assertIn("controlPath(ctx.cwd, branch)", coordinator)
        self.assertIn("sessionPath(ctx.cwd, branch)", coordinator)

        # Hard stop: abort the tool signal so the worker dies quickly, then wait
        # for idle and clear the flag on resume.
        self.assertIn("ctx.abort()", coordinator)
        self.assertIn("waitForIdle", coordinator)
        self.assertIn("clearPause", coordinator)
        self.assertIn("isPaused", coordinator)
        self.assertIn('event.reason === "resume"', coordinator)

        # Resume re-activates the engine tool: pi does not restore the active
        # set from the transcript on resume.
        self.assertIn("function activateTool", coordinator)
        start = coordinator.index('pi.on("session_start"')
        end = coordinator.index('pi.on("session_shutdown"', start)
        start_handler = coordinator[start:end]
        self.assertIn("activateTool()", start_handler)
        command_start = coordinator.index('pi.registerCommand("sliceme"')
        command_end = coordinator.index('pi.registerCommand("suspend"', command_start)
        self.assertIn("activateTool()", coordinator[command_start:command_end])

        # The pause flag gates the recovery path.
        self.assertIn("readJson<any>(controlPath", coordinator)

    def test_resume_prompt_reports_progress(self):
        # Resuming explains the progress (open wave, per-node status) from the
        # engine's resume plan.  A recorded node is finished, so a
        # recorded-but-undelivered campaign reports `ready`.
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        self.assertIn("async function fetchResumePlan", coordinator)
        self.assertIn("function planHasWork", coordinator)
        self.assertIn("function resumePrompt", coordinator)
        self.assertIn("function shutdownStatus", coordinator)
        self.assertIn("function isFinishedStatus", coordinator)
        self.assertIn('status === "recorded"', coordinator)
        self.assertIn("statuses.every(isFinishedStatus)", coordinator)
        self.assertIn("values.filter(isFinishedStatus)", coordinator)
        self.assertIn("Progress: wave", coordinator)
        self.assertIn("Resume plan:", coordinator)
        self.assertIn("resumePrompt(branch, descriptor, plan)", coordinator)

    def test_active_campaign_pointer_is_wired(self):
        # The extension binds a session to one campaign with a per-process
        # pointer file (`docs/multi-campaign.md`).
        common = PI_COMMON.read_text(encoding="utf-8")
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        for symbol in (
            "activeCampaignPath",
            "readActiveCampaign",
            "writeActiveCampaign",
            "clearActiveCampaign",
        ):
            self.assertIn(symbol, common, symbol)
        # The tool applies each reply in `common.ts` (pointer + deliver marker).
        self.assertIn("applyEngineReply(ctx.cwd, action, json)", coordinator)
        self.assertIn("function applyEngineReply", common)
        self.assertIn("clearActiveCampaign(ctx.cwd)", coordinator)

    def test_campaign_resource_unit_test(self):
        # The resolver is pure, so a Node harness drives the fixed grants and the
        # bounded-field rejection. Node 22.6+ strips the TypeScript types.
        node = shutil.which("node")
        harness = REPO_ROOT / "tests" / "campaign_resource_test.mjs"
        if node is None or not harness.is_file():
            self.skipTest("node is not installed")
        if not _node_strip_supported(node):
            self.skipTest("this node cannot strip TypeScript types")
        result = subprocess.run(
            [node, "--no-warnings", "--experimental-strip-types", str(harness)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_active_campaign_pointer_unit_test(self):
        node = shutil.which("node")
        harness = REPO_ROOT / "tests" / "active_campaign_test.mjs"
        if node is None or not harness.is_file():
            self.skipTest("node is not installed")
        if not _node_strip_supported(node):
            self.skipTest("this node cannot strip TypeScript types")
        result = subprocess.run(
            [node, "--no-warnings", "--experimental-strip-types", str(harness)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_deliver_descriptor_unit_test(self):
        # The deliver reply names `target_branch`; the handler must mark the
        # descriptor `completed` without an active-campaign pointer.
        node = shutil.which("node")
        harness = REPO_ROOT / "tests" / "deliver_descriptor_test.mjs"
        if node is None or not harness.is_file():
            self.skipTest("node is not installed")
        if not _node_strip_supported(node):
            self.skipTest("this node cannot strip TypeScript types")
        result = subprocess.run(
            [node, "--no-warnings", "--experimental-strip-types", str(harness)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_typescript_extensions_type_check(self):
        # `npm run typecheck` must pass on the pi extensions. It resolves the pi
        # type declarations from the running pi runtime and exits 3 when neither
        # that nor TypeScript is available, which this test treats as a skip.
        script = REPO_ROOT / "tools" / "typecheck.mjs"
        node = shutil.which("node")
        if node is None or not script.is_file():
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, str(script)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        if result.returncode == 3:
            self.skipTest(result.stderr.strip() or "pi runtime or typescript not available")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_console_scripts(self):
        # The engine is internal: it is invoked from the package, never
        # installed as a user-facing `sliceme` command.
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn("[project.scripts]", pyproject)

    def test_package_ships_the_bundled_cli(self):
        manifest = json.loads(PACKAGE.read_text(encoding="utf-8"))
        files = manifest.get("files", [])
        self.assertIn("bin/", files)
        self.assertIn("sliceme/", files)
        self.assertIn("docs/", files)

    def test_docs_document_the_session_actions(self):
        names = {a.name for a in surface.ACTIONS}
        self.assertIn("status", names)
        self.assertIn("check", names)
        reference = (REPO_ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        for needle in ("--resume", "--sessions"):
            self.assertIn(needle, reference)


if __name__ == "__main__":
    unittest.main()
