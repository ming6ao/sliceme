/**
 * The trusted `sliceme.campaign` workflow resource.
 *
 * `resolve` is synchronous and does no I/O: it validates the bounded args and
 * builds the campaign script and its fixed host commands from literal tokens
 * plus the captured engine path and the optional campaign token. This module is
 * import-free so it can be unit tested without a pi runtime.
 */

export const CAMPAIGN_RESOURCE = "sliceme.campaign";
export const CAMPAIGN_RESOURCE_VERSION = 3;

/** The pi-subagents preview cap for standard output and for standard error. */
export const HOST_PREVIEW_BYTES = 4096;

/** The marker pi-subagents appends when it cuts a host stream. */
export const HOST_CUT_MARKER = "...";

/** How much `record` or `check` text one reviewer task carries. */
export const EVIDENCE_LIMIT = 1600;

/** One worker's hard deadline per tool call (an open tool ends the run). */
export const WORKER_TOOL_TIMEOUT_MS = 600_000;

/** One worker's run deadline. Expiration is terminal. */
export const WORKER_NODE_TIMEOUT_MS = 3_600_000;

/** The runner steers "checkpoint and stop" this long before the kill. */
export const WORKER_CHECKPOINT_MS = 180_000;

/** The single structured status every worker must return. */
export const WORKER_RESULT_SCHEMA = {
	type: "object",
	properties: { status: { type: "string", enum: ["done", "blocked"] } },
	required: ["status"],
	additionalProperties: false,
} as const;

/** The per-node salvage allowance: one relaunch, one new key, then blocked. */
export const SALVAGE_TRY = 2;

/** The only variable text in a generated command. */
export const CAMPAIGN_TOKEN = /^[A-Za-z0-9._-]{1,128}$/;

/** Keys accepted from `args`; every other field is rejected. */
export const CAMPAIGN_RESOURCE_FIELDS = new Set(["campaign", "phaseCap", "nodeCap"]);

export interface CampaignCommands {
	status: string;
	ready: string;
	record: string;
	check: string;
}

export interface CampaignHostGrant {
	key: string;
	command: string;
}

export type CampaignResourceResolution =
	| { script: string; hostCommands: CampaignHostGrant[] }
	| { error: string };

const SAFE_SHELL_TOKEN = /^[A-Za-z0-9_@%+=:,./-]+$/;

/** Quote one token for the POSIX shell the host runs a command in. */
function shellToken(token: string): string {
	if (SAFE_SHELL_TOKEN.test(token)) return token;
	return `'${token.replace(/'/g, `'\\''`)}'`;
}

/** Build one fixed host command from the engine tokens, a verb, and the token. */
export function engineCommand(
	engine: string[],
	verb: readonly string[],
	campaign?: string,
): string {
	const tokens = [...engine, "--json", ...verb];
	if (campaign) tokens.push("--campaign", campaign);
	return tokens.map(shellToken).join(" ");
}

/** The four fixed host commands of the resource. */
export function campaignCommands(engine: string[], campaign?: string): CampaignCommands {
	return {
		status: engineCommand(engine, ["status"], campaign),
		ready: engineCommand(engine, ["ready", "--brief"], campaign),
		record: engineCommand(engine, ["record"], campaign),
		check: engineCommand(engine, ["check"], campaign),
	};
}

/**
 * The authority grants for one campaign. The loop calls each host command
 * once per phase, and the runtime forbids a repeated key, so every iteration
 * gets its own keyed grant (`ready-0`, `record-0`, ...). The final `status`
 * call uses one key. `phaseCap` bounds how many iterations exist.
 *
 * The per-node retry stays inside the loop and grants nothing: the salvage
 * retake resets its own directory with the one `git restore` directive the
 * salvage prompt carries, and the engine derives the try counts from the
 * progress channel. The host authority authorizes exact fixed commands
 * only, so a node id can never ride in a granted command.
 */
export function campaignGrants(
	engine: string[],
	campaign: string | undefined,
	phaseCap: number,
): CampaignHostGrant[] {
	const commands = campaignCommands(engine, campaign);
	const grants: CampaignHostGrant[] = [{ key: "status", command: commands.status }];
	for (let index = 0; index < phaseCap; index++) {
		grants.push({ key: `ready-${index}`, command: commands.ready });
		grants.push({ key: `record-${index}`, command: commands.record });
		grants.push({ key: `check-${index}`, command: commands.check });
	}
	return grants;
}

/**
 * The campaign loop, as a pi-subagents workflow script.
 *
 * The sandbox has no file access, so all engine state arrives as JSON from
 * `runs.host` and all work runs through `runs.run`. The host returns at most
 * `HOST_PREVIEW_BYTES` of each host stream, so the loop reads the `--brief`
 * `ready` form, and each worker reads its own node from the plan file. Every
 * reply passes one guard: a cut payload stops the loop with the verb name, the
 * received size, and the capture path, and it never reaches a reviewer as
 * complete evidence. The loop verifies after `record`: it records the node
 * commits first, then checks and reviews the recorded evidence.
 * `HOST_TIMEOUT_MS` bounds each engine verb, including the synchronous
 * combined-tree `check`. A paused `ready` returns the engine's pause reason, so
 * a budget stop and a user suspension stay distinct.
 */
export function buildCampaignScript(
	commands: CampaignCommands,
	phaseCap: number,
	nodeCap: number,
): string {
	const hostCall = (keyExpr: string, command: string) =>
		`runs.host(${keyExpr}, { kind: "command", command: ${JSON.stringify(command)}, timeoutMs: HOST_TIMEOUT_MS })`;
	return [
		`const PHASE_CAP = ${phaseCap};`,
		`const NODE_CAP = ${nodeCap};`,
		`const HOST_TIMEOUT_MS = 3600000;`,
		`const HOST_PREVIEW_BYTES = ${HOST_PREVIEW_BYTES};`,
		`const HOST_CUT_MARKER = ${JSON.stringify(HOST_CUT_MARKER)};`,
		`const EVIDENCE_LIMIT = ${EVIDENCE_LIMIT};`,
		// The planner writes ASCII-escaped JSON, so the string length equals the
		// byte count. The count is a diagnosis, not a limit test.
		`function hostStdout(res) { return typeof res.stdout === "string" ? res.stdout : ""; }`,
		`function hostCapturePath(res) { return typeof res.outputPath === "string" && res.outputPath ? res.outputPath : "(the host kept no capture path)"; }`,
		`function hostCut(res) { return hostStdout(res).trimEnd().endsWith(HOST_CUT_MARKER); }`,
		`function hostCutError(verb, res) {`,
		`  return new Error("sliceme " + verb + " output is incomplete: the pi-subagents host returns at most " + HOST_PREVIEW_BYTES + " bytes of standard output, and this reply was cut. Received " + hostStdout(res).length + " characters. Full output: " + hostCapturePath(res) + ". Raise the preview cap in the installed pi-subagents bundle to keep a large reply inline.");`,
		`}`,
		`function hostJson(res, verb) {`,
		`  if (hostCut(res)) throw hostCutError(verb, res);`,
		`  try { return JSON.parse(hostStdout(res)); }`,
		`  catch (error) { throw new Error("sliceme " + verb + " returned invalid JSON: " + (error && error.message ? error.message : String(error)) + ". Received " + hostStdout(res).length + " characters. Full output: " + hostCapturePath(res) + "."); }`,
		`}`,
		`function hostEvidence(verb, res) {`,
		`  const text = hostStdout(res);`,
		`  if (!hostCut(res) && text.length <= EVIDENCE_LIMIT) return text;`,
		`  const shown = text.length > EVIDENCE_LIMIT ? text.slice(0, EVIDENCE_LIMIT) : text;`,
		`  return shown + "\\n[" + verb + " output cut: received " + text.length + " characters; full output: " + hostCapturePath(res) + "]";`,
		`}`,
		`const RESULT_STATUS = ${JSON.stringify(WORKER_RESULT_SCHEMA)};`,
		`let phases = 0;`,
		`let nodes = 0;`,
		`const blocked = [];`,
		`function stateDirOf(planPath) {`,
		`  // The plan file sits at <root>/.sliceme/<key>.dag.json, so the parent of
// its parent names the plane root. The child runs in the campaign worktree,
// and the engine's progress channel lives at the root, so the bindings carry
// the absolute state directory.`,
		`  function parent(p) { const i = p.lastIndexOf("/"); return i > 0 ? p.slice(0, i) : p; }`,
		`  return parent(parent(planPath)) + "/.sliceme";`,
		`}`,
		`function childOptions(id, key, keyOfCampaign, stateDir) {`,
		`  return {`,
		`    toolTimeoutMs: ${WORKER_TOOL_TIMEOUT_MS},`,
		`    timeoutMs: ${WORKER_NODE_TIMEOUT_MS},`,
		`    checkpointBeforeDeadlineMs: ${WORKER_CHECKPOINT_MS},`,
		`    extensionBindings: { "sliceme/1": { campaign: keyOfCampaign, node: id, run: key, dir: stateDir } },`,
		`    outputSchema: RESULT_STATUS,`,
		`  };`,
		`}`,
		`function childOutcome(id, result, error) {`,
		`  if (error) {`,
		`    const text = error && error.message ? error.message : String(error);`,
		`    return { id: id, ok: false, reason: text, timedOut: /timed ?-?out|timeout/i.test(text) };`,
		`  }`,
		`  const good = result && result.ok === true && result.structuredOutput && result.structuredOutput.status === "done";`,
		`  return { id: id, ok: good, reason: good ? "" : failureOf({ result: result }), timedOut: result ? result.timedOut === true || result.state === "timedOut" : false };`,
		`}`,
		`function workerTask(node, id, worktree, plan) {`,
		`  const label = node.label ? " (" + node.label + ")" : "";`,
		`  return "Implement Sliceme DAG node '" + id + "'" + label + ".\\nWorktree: " + worktree + "\\nOwned directory: " + (node.dir || "(none)") + "\\nPlan file: " + plan + "\\nRead the plan file and use the entry in its nodes array with id '" + id + "': its goal and its tasks are your instruction. Read no other file under .sliceme/.\\nEdit only files inside the owned directory. Do not run git, do not commit, and do not run the test suite.\\nEnd with your structured status: \\"done\\" when every task is complete, \\"blocked\\" with a report of what stopped you.";`,
		`}`,
		/** The node's bounded progress-tail path: one relative dir per phase. */
		`function tailPathOf(id, progressDir, stateDir) {`,
		`  return progressDir ? stateDir + "/" + progressDir + "/" + id + ".jsonl" : "";`,
		`}`,
		`function salvageTask(node, id, reason, timedOut, worktree, plan, tail) {`,
		`  const lines = [`,
		`    "You are retaking Sliceme DAG node '" + id + "' after your first run failed.",`,
		`    // The terminal reason is unbounded error text: cap it at the same`,
		`    // reviewer evidence budget the plan gives the salvage prompt.`,
		`    "Your first run ended with: " + reason.slice(0, EVIDENCE_LIMIT) + ".",`,
		`    "First read your own bounded progress tail: " + (tail || "(no event reached the channel for this node)"),`,
		`  ];`,
		`  // A node that declares no directory owns no partial work to restore, and`,
		`  // a reset at the worktree root would destroy every sibling's work, so a`,
		`  // no-directory retake carries no reset directive at all.`,
		`  if (node.dir) {`,
		`    lines.push("Then reset your own partial work with exactly: git restore --source=HEAD -- " + node.dir + ". That one git command is ordered by the coordinator for this retake only; run git for nothing else, and touch no sibling work.");`,
		`  }`,
		`  if (timedOut) {`,
		`    lines.push("Your command or tool call hung once; run it again with a short timeout first.");`,
		`  } else {`,
		`    lines.push("Finish every task in order before you end.");`,
		`  }`,
		`  lines.push(workerTask(node, id, worktree, plan));`,
		`  return lines.join("\\n");`,
		`}`,
		`function failureOf(job) {`,
		`  if (job.reason) return job.reason;`,
		`  const result = job.result;`,
		`  if (result && result.structuredOutput && result.structuredOutput.status === "blocked") return "the child ended with status blocked";`,
		`  if (result && (result.timedOut === true || result.state === "timedOut")) return "timedOut";`,
		`  return "the child ended without status done" + (result && result.error ? ": " + String(result.error) : "");`,
		`}`,
		`while (true) {`,
		`  const readyRes = await ${hostCall('"ready-" + phases', commands.ready)};`,
		`  if (!readyRes.ok) throw new Error("sliceme ready failed: " + (readyRes.error || readyRes.stderr || readyRes.exitCode));`,
		`  const ready = hostJson(readyRes, "ready");`,
		`  if (ready.paused) {`,
		`    return { state: "paused", phase: ready.phase === undefined ? null : ready.phase, reason: ready.pause_reason === undefined ? null : ready.pause_reason, metric: ready.pause_metric === undefined ? null : ready.pause_metric, phases: phases, nodes: nodes };`,
		`  }`,
		`  const ids = Array.isArray(ready.ready) ? ready.ready : [];`,
		`  if (ids.length === 0) break;`,
		`  const worktree = ready.worktree;`,
		`  if (!worktree) throw new Error("sliceme: the campaign worktree is missing; run sliceme open first");`,
		`  const plan = typeof ready.dag === "string" ? ready.dag : "";`,
		`  if (!plan) throw new Error("sliceme: the ready reply has no plan path; run sliceme start first");`,
		`  const definitions = Array.isArray(ready.nodes) ? ready.nodes : [];`,
		`  const byId = new Map(definitions.map(function (node) { return [node.id, node]; }));`,
		`  const branchKey = typeof ready.branch_key === "string" && ready.branch_key ? ready.branch_key : "";`,
		`  const stateDir = stateDirOf(plan);`,
		`  // The brief reply names the channel once, relative to its state dir.`,
		`  const progressDir = typeof ready.progress_dir === "string" && ready.progress_dir ? ready.progress_dir : "";`,
		`  const jobs = ids.map(function (id) {`,
		`    const node = byId.get(id) || {};`,
		`    const key = "node-" + id;`,
		`    return runs.run(key, Object.assign({`,
		`      agent: "sliceme-worker",`,
		`      cwd: worktree,`,
		`      task: workerTask(node, id, worktree, plan),`,
		`    }, childOptions(id, key, branchKey, stateDir))).then(`,
		`      function (result) { return childOutcome(id, result, null); },`,
		`      function (error) { return childOutcome(id, null, error); },`,
		`    );`,
		`  });`,
		`  const settled = await Promise.all(jobs);`,
		`  nodes += ids.length;`,
		`  const failed = settled.filter(function (outcome) { return !outcome.ok; });`,
		`  const spend = ready.spend || {};`,
		`  if (failed.length > 0 && spend.retry === false) {`,
		`    for (const job of failed) {`,
		`      blocked.push({ node: job.id, reason: "the campaign spend is at its cap", tries: 1 });`,
		`    }`,
		`  } else if (failed.length > 0) {`,
		`    const retakes = await Promise.all(failed.map(function (job) {`,
		`      const node = byId.get(job.id) || {};`,
		`      const key = "node-" + job.id + "-try-2";`,
		`      return runs.run(key, Object.assign({`,
		`        agent: "sliceme-worker",`,
		`        cwd: worktree,`,
		`        task: salvageTask(node, job.id, failureOf(job), job.timedOut === true, worktree, plan, tailPathOf(job.id, progressDir, stateDir)),`,
		`      }, childOptions(job.id, key, branchKey, stateDir))).then(`,
		`        function (result) { return childOutcome(job.id, result, null); },`,
		`        function (error) { return childOutcome(job.id, null, error); },`,
		`      );`,
		`    }));`,
		`    for (const retake of retakes) {`,
		`      if (!retake.ok) {`,
		`        blocked.push({ node: retake.id, reason: failureOf(retake), tries: 2 });`,
		`      }`,
		`    }`,
		`  }`,
		`  const recordRes = await ${hostCall('"record-" + phases', commands.record)};`,
		`  if (!recordRes.ok) throw new Error("sliceme record failed: " + (recordRes.error || recordRes.stderr || recordRes.exitCode));`,
		`  const checkRes = await ${hostCall('"check-" + phases', commands.check)};`,
		`  if (!checkRes.ok) throw new Error("sliceme check failed: " + (checkRes.error || checkRes.stderr || checkRes.exitCode));`,
		`  const reviewTask = "Review the recorded phase evidence for Sliceme phase " + (ready.phase === undefined ? "?" : ready.phase) + ".\\nRecorded: " + hostEvidence("record", recordRes) + "\\nChecks: " + hostEvidence("check", checkRes) + "\\nReport concrete findings, or state that the phase is clean. Do not edit files.";`,
		`  await runs.run("review-" + (ready.phase === undefined ? phases : ready.phase), { agent: "reviewer", cwd: worktree, task: reviewTask });`,
		`  phases += 1;`,
		`  if (blocked.length > 0) {`,
		`    return { state: "blocked", blocked: blocked, phases: phases, nodes: nodes, phase: ready.phase === undefined ? null : ready.phase, next: "resolve the blocked nodes, then run the sliceme.campaign resource again" };`,
		`  }`,
		`  if ((PHASE_CAP > 0 && phases >= PHASE_CAP) || (NODE_CAP > 0 && nodes >= NODE_CAP)) {`,
		`    return { state: "capped", phases: phases, nodes: nodes, blocked: blocked, next: "run the sliceme.campaign resource again to continue or confirm completion" };`,
		`  }`,
		`}`,
		`const statusRes = await ${hostCall('"status"', commands.status)};`,
		`let summary = null;`,
		`let statusError = null;`,
		`if (statusRes.ok) {`,
		`  try { summary = hostJson(statusRes, "status"); }`,
		`  catch (error) { statusError = error && error.message ? error.message : String(error); }`,
		`} else {`,
		`  statusError = "the status command failed: " + (statusRes.error || statusRes.stderr || statusRes.exitCode);`,
		`}`,
		`return { state: "complete", phases: phases, nodes: nodes, blocked: blocked, status: summary, status_error: statusError };`,
	].join("\n");
}

/**
 * Validate the resource args and expand them into the campaign script.
 *
 * Synchronous and bounded: it reads the captured engine tokens, validates every
 * field, and builds each command from fixed literals plus the campaign token.
 * It performs no I/O.
 */
export function resolveCampaignResource(
	args: Readonly<Record<string, unknown>>,
	engine: string[],
): CampaignResourceResolution {
	const unknown = Object.keys(args).filter((key) => !CAMPAIGN_RESOURCE_FIELDS.has(key));
	if (unknown.length) {
		return { error: `workflow '${CAMPAIGN_RESOURCE}' args contain unsupported fields: ${unknown.join(", ")}.` };
	}
	if (engine.length === 0) {
		return { error: `workflow '${CAMPAIGN_RESOURCE}' has no resolved engine path.` };
	}
	let campaign: string | undefined;
	if (args.campaign !== undefined) {
		if (typeof args.campaign !== "string" || !CAMPAIGN_TOKEN.test(args.campaign)) {
			return { error: `workflow '${CAMPAIGN_RESOURCE}' args.campaign must match [A-Za-z0-9._-]{1,128}.` };
		}
		campaign = args.campaign;
	}
	const phaseCap = args.phaseCap;
	if (
		phaseCap !== undefined &&
		(typeof phaseCap !== "number" ||
			!Number.isInteger(phaseCap) ||
			phaseCap < 1 ||
			phaseCap > 10)
	) {
		return {
			error: `workflow '${CAMPAIGN_RESOURCE}' args.phaseCap must be an integer from 1 to 10; the host allows at most 32 authority grants and each phase needs three. Run the resource again to continue a longer plan.`,
		};
	}
	const nodeCap = args.nodeCap;
	if (
		nodeCap !== undefined &&
		(typeof nodeCap !== "number" || !Number.isInteger(nodeCap) || nodeCap < 1 || nodeCap > 256)
	) {
		return { error: `workflow '${CAMPAIGN_RESOURCE}' args.nodeCap must be an integer from 1 to 256.` };
	}
	const commands = campaignCommands(engine, campaign);
	const boundedPhaseCap = typeof phaseCap === "number" ? phaseCap : 10;
	return {
		script: buildCampaignScript(
			commands,
			boundedPhaseCap,
			typeof nodeCap === "number" ? nodeCap : 256,
		),
		hostCommands: campaignGrants(engine, campaign, boundedPhaseCap),
	};
}
