/**
 * The trusted `sliceme.campaign` workflow resource.
 *
 * `resolve` is synchronous and does no I/O: it validates the bounded args and
 * builds the campaign script and its fixed host commands from literal tokens
 * plus the captured engine path and the optional campaign token. This module is
 * import-free so it can be unit tested without a pi runtime.
 */

export const CAMPAIGN_RESOURCE = "sliceme.campaign";
export const CAMPAIGN_RESOURCE_VERSION = 1;

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
		ready: engineCommand(engine, ["ready"], campaign),
		record: engineCommand(engine, ["record"], campaign),
		check: engineCommand(engine, ["check"], campaign),
	};
}

/**
 * The authority grants for one campaign. The loop calls each host command
 * once per phase, and the runtime forbids a repeated key, so every iteration
 * gets its own keyed grant (`ready-0`, `record-0`, ...). The final `status`
 * call uses one key. `phaseCap` bounds how many iterations exist.
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
 * `runs.host` and all work runs through `runs.run`. The loop verifies after
 * `record`: it records the node commits first, then checks and reviews the
 * recorded evidence. `HOST_TIMEOUT_MS` bounds each engine verb, including the
 * synchronous combined-tree `check`. A paused `ready` returns the engine's
 * pause reason, so a budget stop and a user suspension stay distinct.
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
		`let phases = 0;`,
		`let nodes = 0;`,
		`while (true) {`,
		`  const readyRes = await ${hostCall('"ready-" + phases', commands.ready)};`,
		`  if (!readyRes.ok) throw new Error("sliceme ready failed: " + (readyRes.error || readyRes.stderr || readyRes.exitCode));`,
		`  const ready = JSON.parse(readyRes.stdout);`,
		`  if (ready.paused) {`,
		`    return { state: "paused", phase: ready.phase === undefined ? null : ready.phase, reason: ready.pause_reason === undefined ? null : ready.pause_reason, metric: ready.pause_metric === undefined ? null : ready.pause_metric, phases: phases, nodes: nodes };`,
		`  }`,
		`  const ids = Array.isArray(ready.ready) ? ready.ready : [];`,
		`  if (ids.length === 0) break;`,
		`  const worktree = ready.worktree;`,
		`  if (!worktree) throw new Error("sliceme: the campaign worktree is missing; run sliceme open first");`,
		`  const definitions = Array.isArray(ready.nodes) ? ready.nodes : [];`,
		`  const byId = new Map(definitions.map(function (node) { return [node.id, node]; }));`,
		`  const workers = ids.map(function (id) {`,
		`    const node = byId.get(id) || {};`,
		`    const tasks = Array.isArray(node.tasks) && node.tasks.length ? "\\n- " + node.tasks.join("\\n- ") : "\\n- (none)";`,
		`    return {`,
		`      key: "node-" + id,`,
		`      agent: "sliceme-worker",`,
		`      cwd: worktree,`,
		`      task: "Implement Sliceme DAG node '" + id + "'.\\nWorktree: " + worktree + "\\nOwned directory: " + (node.dir || "(none)") + "\\nGoal: " + (node.goal || "(none)") + "\\nTasks:" + tasks + "\\nEdit only files inside the owned directory. Do not run git, do not commit, and do not run the test suite."`,
		`    };`,
		`  });`,
		`  await runs.all(workers);`,
		`  const recordRes = await ${hostCall('"record-" + phases', commands.record)};`,
		`  if (!recordRes.ok) throw new Error("sliceme record failed: " + (recordRes.error || recordRes.stderr || recordRes.exitCode));`,
		`  const checkRes = await ${hostCall('"check-" + phases', commands.check)};`,
		`  if (!checkRes.ok) throw new Error("sliceme check failed: " + (checkRes.error || checkRes.stderr || checkRes.exitCode));`,
		`  const reviewTask = "Review the recorded phase evidence for Sliceme phase " + (ready.phase === undefined ? "?" : ready.phase) + ".\\nRecorded: " + recordRes.stdout + "\\nChecks: " + checkRes.stdout + "\\nReport concrete findings, or state that the phase is clean. Do not edit files.";`,
		`  await runs.run("review-" + (ready.phase === undefined ? phases : ready.phase), { agent: "reviewer", cwd: worktree, task: reviewTask });`,
		`  phases += 1;`,
		`  nodes += ids.length;`,
		`  if ((PHASE_CAP > 0 && phases >= PHASE_CAP) || (NODE_CAP > 0 && nodes >= NODE_CAP)) {`,
		`    return { state: "capped", phases: phases, nodes: nodes, next: "run the sliceme.campaign resource again to continue or confirm completion" };`,
		`  }`,
		`}`,
		`const statusRes = await ${hostCall('"status"', commands.status)};`,
		`let summary = null;`,
		`if (statusRes.ok) { try { summary = JSON.parse(statusRes.stdout); } catch (error) { summary = statusRes.stdout; } }`,
		`return { state: "complete", phases: phases, nodes: nodes, status: summary };`,
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
