"""Guards the pi package contract.

Sliceme supports exactly one install path: ``pi install`` of this package, which
registers one extension exposing the ``sliceme``/``sliceme-unit`` tools and the
``/sliceme`` command. There is no separate skill. These tests fail if that
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
PI_UNIT = PI_DIR / "unit.ts"
PI_COORDINATOR = PI_DIR / "coordinator.ts"
PI_COMMON = PI_DIR / "common.ts"

sys.path.insert(0, str(REPO_ROOT))
from sliceme import surface  # noqa: E402


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise AssertionError("file is missing YAML frontmatter")
    data: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.startswith((" ", "\t", "#")):
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip().strip('"')
    return data


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
        # No skill: the extension owns the `/sliceme` command and both tools.
        manifest = json.loads(PACKAGE.read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("pi", {}).get("skills", []), [])
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        self.assertIn('pi.registerCommand("sliceme"', coordinator)
        self.assertIn("sendUserMessage", coordinator)
        # The command activates both tools for the session.
        self.assertIn("setActiveTools", coordinator)
        # The old skill-trigger glue is gone.
        self.assertNotIn("pi.on(\"input\"", coordinator)
        self.assertNotIn("/skill:sliceme", coordinator)
        self.assertNotIn("SKILL_INVOCATION", coordinator)
        self.assertNotIn("hasDesignDocument", coordinator)

    def test_tools_register_inactive_and_are_activated_by_the_command(self):
        # Re-gated: a plain session does not advertise Sliceme; `/sliceme` turns
        # both tools on via `pi.setActiveTools`.
        for path in (PI_UNIT, PI_COORDINATOR):
            text = path.read_text(encoding="utf-8")
            self.assertIn("defaultActive: false", text, path)
            self.assertNotIn("defaultActive: true", text, path)

    def test_pi_extension_is_a_thin_forwarder(self):
        # There is no single-agent bootstrap: the extension only registers the
        # `sliceme-unit` tool and forwards to the CLI via the shared helpers.
        text = PI_UNIT.read_text(encoding="utf-8")
        self.assertIn('from "./common.ts"', text)
        self.assertIn("runSliceme(pi, ctx", text)
        self.assertNotIn("bindingIsStale", text)
        self.assertNotIn("bootstrap(ctx)", text)
        self.assertNotIn("before_agent_start", text)

    def test_pi_package_manifest(self):
        self.assertTrue(PACKAGE.is_file(), "package.json is required for `pi install`")
        manifest = json.loads(PACKAGE.read_text(encoding="utf-8"))
        self.assertIn("pi-package", manifest.get("keywords", []))
        pi = manifest.get("pi", {})
        extensions = pi.get("extensions", [])
        self.assertIn("./integrations/pi/unit.ts", extensions)
        self.assertIn("./integrations/pi/coordinator.ts", extensions)
        # The shared helpers ship with the package and are imported by both tools.
        self.assertTrue(PI_COMMON.is_file())
        self.assertIn('from "./common.ts"', PI_COORDINATOR.read_text(encoding="utf-8"))

    def test_subagent_tools_are_scoped(self):
        # runSubagent must pass the agent's `tools:` allowlist to `pi --tools`;
        # this keeps workers on the `sliceme-unit` tool and away from the
        # `sliceme` coordinator tool, and gives the read-only verifier neither.
        common = PI_COMMON.read_text(encoding="utf-8")
        self.assertIn('"--tools"', common)
        self.assertIn("agentFrontmatterValue", common)
        worker = _frontmatter((PI_DIR / "agents" / "worker.md").read_text(encoding="utf-8"))
        worker_tools = [t.strip() for t in worker["tools"].split(",")]
        self.assertIn("sliceme-unit", worker_tools)
        self.assertNotIn("sliceme", worker_tools)
        verifier = _frontmatter((PI_DIR / "agents" / "verifier.md").read_text(encoding="utf-8"))
        verifier_tools = [t.strip() for t in verifier["tools"].split(",")]
        self.assertNotIn("sliceme-unit", verifier_tools)
        self.assertNotIn("bash", verifier_tools, "the verifier must not run commands")

    def test_campaign_executor_and_gate_wiring(self):
        # The coordinator drives the single executor and the sandbox gate, and
        # the verifier judges recorded evidence rather than running a suite.
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        self.assertIn('"exec"', coordinator)
        self.assertIn("sandboxGate", coordinator)
        self.assertIn('["exec", "--validate"]', coordinator)
        self.assertIn("EXEC_KEYS", coordinator)
        verifier = (PI_DIR / "agents" / "verifier.md").read_text(encoding="utf-8")
        self.assertNotIn("tools/gpu.sh", verifier)
        self.assertIn("executor", verifier)

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

    def test_retired_actions_are_gone(self):
        # The single-agent path (`handoff`), the old `submit`/`verify` verbs, and
        # the per-wave `integrate` landing action, and the plain-plane `commit`
        # action are retired; `deliver` is the end-of-campaign pull request and
        # `review` is the local review surface.
        names = {a.name for a in surface.ACTIONS}
        for gone in ("submit", "verify", "handoff", "declare", "integrate", "commit"):
            self.assertNotIn(gone, names)
        self.assertIn("deliver", names)
        self.assertIn("review", names)
        self.assertIn("wave", names)
        text = PI_UNIT.read_text(encoding="utf-8")
        for gone in ("submit", "verify", "handoff", "declare", "integrate", "commit"):
            self.assertNotIn(f'"{gone}"', text)

    def test_campaign_actions_are_in_lockstep(self):
        names = [a.name for a in surface.ACTIONS]
        self.assertIn("deliver", names)
        self.assertIn("wave", names)
        self.assertIn("review", names)
        self.assertIn("progress", names)
        text = PI_UNIT.read_text(encoding="utf-8")
        match = re.search(r"SLICEME_ACTIONS\s*=\s*\[(.*?)\]\s*as const", text, re.DOTALL)
        self.assertIsNotNone(match)
        self.assertEqual(re.findall(r'"([a-z_]+)"', match.group(1)), names)
        # The workflow doc documents the campaign additions.
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("`deliver`", workflow)
        self.assertIn("`wave`", workflow)
        self.assertIn("`review`", workflow)
        self.assertIn("--no-unit", workflow)

    def test_campaign_scheduler_is_wave_aware(self):
        # The coordinator projects the DAG into waves via the engine and gates
        # spawns on the current wave; the wave planner is a first-class module.
        coordinator_text = PI_COORDINATOR.read_text(encoding="utf-8")
        for needle in ("dag_waves", "currentWave", "readyWaveNodes", "refreshWaves"):
            self.assertIn(needle, coordinator_text)
        self.assertTrue((REPO_ROOT / "sliceme" / "ownership.py").is_file())
        self.assertTrue((REPO_ROOT / "tests" / "test_waves.py").is_file())

    def test_commit_subjects_are_descriptions(self):
        # A wave commit subject is the human description, never a wave prefix.
        # The coordinator captures the worker report and passes a per-node map.
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        for needle in ("function workerDescription", "function nodeDescriptions", '"--messages"'):
            self.assertIn(needle, coordinator)

    def test_session_suspend_resume_contract(self):
        # The adapter writes the descriptor, hard-aborts the in-flight turn to
        # suspend quickly, resumes on the pi session_start hook, and never shadows
        # pi's `/resume`.
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        common = PI_COMMON.read_text(encoding="utf-8")

        # Descriptor / pause / heartbeat helpers live in common.ts.
        for needle in ("export function sessionPath", "export function controlPath", "export function heartbeatPath"):
            self.assertIn(needle, common)
        self.assertIn("heartbeat?: string", common)

        # The on-disk heartbeat uses the `.sliceme/` snake_case convention that
        # docs/observability.md documents, and the continuation reader matches.
        self.assertIn("tool_calls: snapshot.toolCalls", common)
        self.assertIn("heartbeat.tool_calls", coordinator)

        # Commands and lifecycle hooks.
        self.assertIn('pi.registerCommand("suspend"', coordinator)
        self.assertIn('pi.registerCommand("campaigns"', coordinator)
        self.assertIn('pi.on("session_start"', coordinator)
        self.assertIn('pi.on("session_shutdown"', coordinator)
        self.assertNotIn('pi.registerCommand("resume"', coordinator)
        self.assertIn("writeSessionDescriptor", coordinator)
        self.assertIn("controlPath(ctx.cwd, branch)", coordinator)
        self.assertIn("sessionPath(ctx.cwd, branch)", coordinator)
        self.assertIn("heartbeatPath(ctx.cwd, branch", coordinator)

        # Hard stop: abort the tool signal so the worker dies quickly, then wait
        # for idle and clear the flag on resume.  Steering is deliberately gone:
        # it only arrives at the next turn boundary, after the node has finished.
        self.assertIn("ctx.abort()", coordinator)
        self.assertIn("waitForIdle", coordinator)
        self.assertNotIn('deliverAs: "steer"', coordinator)
        self.assertIn("clearPause", coordinator)
        self.assertIn("isPaused", coordinator)
        self.assertIn('event.reason === "resume"', coordinator)

        # Resume must re-activate the campaign tools. Pi does not restore the
        # active set from the transcript on resume, so the injected prompt would
        # otherwise tell the model to call a tool that is not declared. Both the
        # `/sliceme` command and the `session_start` hook share one helper.
        self.assertIn("function activateCampaignTools", coordinator)
        start = coordinator.index('pi.on("session_start"')
        end = coordinator.index('pi.on("session_shutdown"', start)
        start_handler = coordinator[start:end]
        self.assertIn("activateCampaignTools()", start_handler)
        command_start = coordinator.index('pi.registerCommand("sliceme"')
        command_end = coordinator.index('pi.registerCommand("suspend"', command_start)
        self.assertIn("activateCampaignTools()", coordinator[command_start:command_end])

        # A signal-killed worker is reported as interrupted and mapped to a
        # paused node so resume continues its edits instead of respawning.
        self.assertIn("interrupted?: boolean", common)
        self.assertIn("interrupted: signal != null", common)
        self.assertIn("result.interrupted", coordinator)

        # The pause flag gates every work-performing path, and an interrupted
        # worker or verifier (a second `pausedResult` after the subagent) is
        # mapped to a paused result too.
        for action in ("spawn", "record", "verify", "ready"):
            self.assertIn(f'pausedResult("{action}")', coordinator)
        self.assertGreaterEqual(coordinator.count('pausedResult("verify")'), 2)

        # Attempts and switchSession are wired.
        self.assertIn('"--begin"', coordinator)
        self.assertIn('"--end"', coordinator)
        self.assertIn("switchSession", coordinator)

    def test_resume_prompt_reports_progress(self):
        # Resuming explains the progress (open wave, wave count, per-node status)
        # from the engine's resume plan, instead of telling the model to run
        # `status` to find out.
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        self.assertIn("async function fetchResumePlan", coordinator)
        self.assertIn("function planHasWork", coordinator)
        self.assertIn(
            "function resumePrompt(branch: string, descriptor: any, plan: any)", coordinator
        )
        self.assertIn("Progress: wave", coordinator)
        self.assertIn('lines.push("Waves:")', coordinator)
        self.assertIn("Resume plan:", coordinator)
        self.assertIn("resumePrompt(branch, descriptor, plan)", coordinator)
        self.assertNotIn('action "status" to see the plan', coordinator)

    def test_review_server_lifecycle_is_bounded(self):
        # The review server starts after the first recorded commit and stops
        # after delivery.  A delivered campaign must not restart it.  The URL
        # file is per session, and a parent-death pipe stops an orphan server.
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        common = PI_COMMON.read_text(encoding="utf-8")

        self.assertIn("function reviewNeeded", coordinator)
        self.assertIn("!state.delivered", coordinator)
        self.assertIn("stopReviewServer(ctx, branch)", coordinator)
        self.assertIn('"review.stopped"', coordinator)

        self.assertIn("export function cleanStaleReviewUrls", common)
        self.assertIn("review.${pid}.url", common)
        self.assertIn('stdio: ["pipe", "ignore", err]', common)

        review_doc = (REPO_ROOT / "docs" / "review.md").read_text(encoding="utf-8")
        self.assertIn("Server lifecycle:", review_doc)

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

    def test_live_progress_view_is_wired(self):
        # The live multi-subagent view (docs/observability.md Priority 0): the
        # coordinator owns one registry and one render timer, the reducer reaches
        # it through `onProgress`, and `spawn` streams its own row via `onUpdate`.
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        common = PI_COMMON.read_text(encoding="utf-8")
        for needle in (
            "liveAgents",
            "ensureLiveTimer",
            "stopLiveTimer",
            "renderProgress",
            "renderAgentLine",
            "onProgress:",
            "onUpdate",
        ):
            self.assertIn(needle, coordinator, needle)
        self.assertIn("export function renderProgress", common)
        self.assertIn("export function renderAgentLine", common)
        self.assertIn("export function truncateToWidth", common)
        # Every recognized stream event reaches the live view; only the heartbeat
        # file stays debounced.
        self.assertIn("emitProgress", common)
        # The timer stops on session shutdown.
        self.assertIn("stopLiveTimer()", coordinator)
        # The metrics line and the tool and thinking split (docs/observability.md §3).
        for needle in ("buildMetricsLine", "showMetrics", "toolSeconds", "toolStartedAt"):
            self.assertIn(needle, common, needle)

    def test_metrics_pipeline_is_wired(self):
        # The reducer pairs tool calls by id, and the coordinator persists the
        # metrics to `attempt --end` (docs/observability.md §2).
        common = PI_COMMON.read_text(encoding="utf-8")
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        for needle in ("reduceToolStart", "reduceToolEnd", "programName", "topCommands"):
            self.assertIn(f"export function {needle}", common, needle)
        for needle in (
            "tool_seconds",
            "tool_durations",
            "slowest_commands",
        ):
            self.assertIn(needle, common, needle)
        for needle in (
            '"--tool-seconds"',
            '"--tool-durations"',
            '"--slowest-commands"',
            '"--turns"',
            '"--tokens-in"',
        ):
            self.assertIn(needle, coordinator, needle)

    def test_metrics_unit_test(self):
        # The metrics helpers are pure, so a Node harness drives them with a
        # fixed event list. Node 22.6+ strips the TypeScript types at run time.
        node = shutil.which("node")
        harness = REPO_ROOT / "tests" / "metrics_test.mjs"
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

    def test_render_progress_unit_test(self):
        # The renderer is pure, so a Node harness drives it with fixed snapshots
        # and widths. Node 22.6+ strips the TypeScript types at run time.
        node = shutil.which("node")
        harness = REPO_ROOT / "tests" / "render_progress_test.mjs"
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

    def test_campaign_state_store_is_wired(self):
        # One in-process store owns `state.json`; parallel spawn completions share
        # the state object, so none of them drops another's node status
        # (docs/observability.md §9 suggestion 2).
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        common = PI_COMMON.read_text(encoding="utf-8")
        self.assertIn("class CampaignStateStore", common)
        self.assertIn("export function writeJson", common)
        self.assertIn("function stateStore", coordinator)
        self.assertIn("new CampaignStateStore(file)", coordinator)
        # No read-modify-write of the state file remains in the coordinator.
        self.assertNotIn("readJson(stateFile", coordinator)
        self.assertNotIn("writeJson(stateFile", coordinator)
        self.assertNotIn("writeJson(statePath", coordinator)

    def test_state_store_unit_test(self):
        # The store keeps two parallel updates, persists atomically, and honors
        # `save(false)` after delivery removed the file.
        node = shutil.which("node")
        harness = REPO_ROOT / "tests" / "state_store_test.mjs"
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

    def test_active_campaign_pointer_is_wired(self):
        # The coordinator binds a session to one campaign with a per-process
        # pointer file, and passes `--campaign` to campaign-scoped engine calls
        # (docs/multi-campaign.md).
        common = PI_COMMON.read_text(encoding="utf-8")
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        for symbol in (
            "activeCampaignPath",
            "readActiveCampaign",
            "writeActiveCampaign",
            "clearActiveCampaign",
        ):
            self.assertIn(symbol, common, symbol)
        self.assertIn("function activeCampaign", coordinator)
        self.assertIn("withCampaign", coordinator)
        self.assertIn("writeActiveCampaign(ctx.cwd, branch)", coordinator)
        self.assertIn('"--campaign"', coordinator)
        self.assertIn("clearActiveCampaign(ctx.cwd)", coordinator)

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

    def test_docs_document_the_session_actions(self):
        names = {a.name for a in surface.ACTIONS}
        self.assertIn("attempt", names)
        self.assertIn("status", names)
        self.assertIn("progress", names)
        reference = (REPO_ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        for needle in ("`attempt`", "`progress`", "--resume", "--sessions", "--tool-seconds"):
            self.assertIn(needle, reference)
        database = (REPO_ROOT / "docs" / "database.md").read_text(encoding="utf-8")
        self.assertIn("attempts", database)
        self.assertIn("tool_seconds", database)


if __name__ == "__main__":
    unittest.main()
