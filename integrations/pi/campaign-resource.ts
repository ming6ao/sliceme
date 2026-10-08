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
export const CAMPAIGN_RESOURCE_FIELDS = new Set(["campaign", "waveCap", "nodeCap"]);

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
function engineCommand(engine: string[], verb: readonly string[], campaign?: string): string {
	const tokens = [...engine, "--json", ...verb];
	if (campaign) tokens.push("--campaign", campaign);
	return tokens.map(shellToken).join(" ");
}

/** The four fixed host commands of the resource. */
export function campaignCommands(engine: string[], campaign?: string): CampaignCommands {
	return {
		status: engineCommand(engine, ["status"], campaign),
		ready: engineCommand(engine, ["ready"], campaign),
		record: engineCommand(engine, ["wave", "--record", "--current"], campaign),
		check: engineCommand(engine, ["check", "--current"], campaign),
	};
}

/**
 * The campaign loop, as a pi-subagents workflow script.
 *
 * The sandbox has no file access, so all engine state arrives as JSON from
 * `runs.host` and all work runs through `runs.run`. The loop verifies after
 * `record`: it records the node commits first, then checks and reviews the
 * recorded evidence. `HOST_TIMEOUT_MS` bounds each engine verb, including the
 * synchronous combined-tree `check`.
 */
export function buildCampaignScript(
	commands: CampaignCommands,
	waveCap: number,
	nodeCap: number,
): string {
	const hostCall = (key: string, command: string) =>
		`runs.host(${JSON.stringify(key)}, { kind: "command", command: ${JSON.stringify(command)}, timeoutMs: HOST_TIMEOUT_MS })`;
	return [
		`const WAVE_CAP = ${waveCap};`,
		`const NODE_CAP = ${nodeCap};`,
		`const HOST_TIMEOUT_MS = 3600000;`,
		`let waves = 0;`,
		`let nodes = 0;`,
		`while (true) {`,
		`  const readyRes = await ${hostCall("ready", commands.ready)};`,
		`  if (!readyRes.ok) throw new Error("sliceme ready failed: " + (readyRes.error || readyRes.stderr || readyRes.exitCode));`,
		`  const ready = JSON.parse(readyRes.stdout);`,
		`  if (ready.paused) {`,
		`    return { state: "paused", wave: ready.wave === undefined ? null : ready.wave, waves: waves, nodes: nodes };`,
		`  }`,
		`  const ids = Array.isArray(ready.ready) ? ready.ready : [];`,
		`  if (ids.length === 0) break;`,
		`  const workers = ids.map(function (id) {`,
		`    return {`,
		`      key: "node-" + id,`,
		`      agent: "sliceme-worker",`,
		`      task: "Implement Sliceme DAG node '" + id + "'. Read the campaign DAG under .sliceme/ to find the node, edit only files inside its owned directories, then stop. Do not run git, do not commit, and do not run the test suite."`,
		`    };`,
		`  });`,
		`  await runs.all(workers);`,
		`  const recordRes = await ${hostCall("record", commands.record)};`,
		`  if (!recordRes.ok) throw new Error("sliceme record failed: " + (recordRes.error || recordRes.stderr || recordRes.exitCode));`,
		`  const checkRes = await ${hostCall("check", commands.check)};`,
		`  if (!checkRes.ok) throw new Error("sliceme check failed: " + (checkRes.error || checkRes.stderr || checkRes.exitCode));`,
		`  const reviewTask = "Review the recorded wave evidence for Sliceme wave " + (ready.wave === undefined ? "?" : ready.wave) + ".\\nRecorded: " + recordRes.stdout + "\\nChecks: " + checkRes.stdout + "\\nReport concrete findings, or state that the wave is clean. Do not edit files.";`,
		`  await runs.run("review-" + (ready.wave === undefined ? waves : ready.wave), { agent: "reviewer", task: reviewTask });`,
		`  waves += 1;`,
		`  nodes += ids.length;`,
		`  if ((WAVE_CAP > 0 && waves >= WAVE_CAP) || (NODE_CAP > 0 && nodes >= NODE_CAP)) {`,
		`    return { state: "capped", waves: waves, nodes: nodes };`,
		`  }`,
		`}`,
		`const statusRes = await ${hostCall("status", commands.status)};`,
		`let summary = null;`,
		`if (statusRes.ok) { try { summary = JSON.parse(statusRes.stdout); } catch (error) { summary = statusRes.stdout; } }`,
		`return { state: "complete", waves: waves, nodes: nodes, status: summary };`,
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
	const waveCap = args.waveCap;
	if (
		waveCap !== undefined &&
		(typeof waveCap !== "number" ||
			!Number.isInteger(waveCap) ||
			waveCap < 1 ||
			waveCap > 64)
	) {
		return { error: `workflow '${CAMPAIGN_RESOURCE}' args.waveCap must be an integer from 1 to 64.` };
	}
	const nodeCap = args.nodeCap;
	if (
		nodeCap !== undefined &&
		(typeof nodeCap !== "number" || !Number.isInteger(nodeCap) || nodeCap < 1 || nodeCap > 256)
	) {
		return { error: `workflow '${CAMPAIGN_RESOURCE}' args.nodeCap must be an integer from 1 to 256.` };
	}
	const commands = campaignCommands(engine, campaign);
	return {
		script: buildCampaignScript(
			commands,
			typeof waveCap === "number" ? waveCap : 64,
			typeof nodeCap === "number" ? nodeCap : 256,
		),
		hostCommands: [
			{ key: "status", command: commands.status },
			{ key: "ready", command: commands.ready },
			{ key: "record", command: commands.record },
			{ key: "check", command: commands.check },
		],
	};
}
