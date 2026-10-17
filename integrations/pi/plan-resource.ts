/**
 * The trusted `sliceme.plan` workflow resource.
 *
 * The coordinator launches this resource after `start` and before `open`. The
 * resource runs the `sliceme-planner` agent, validates the DAG through the
 * engine, and runs the planner again with the engine error text. The loop is
 * bounded to three passes. It returns `planned` for the first valid DAG and
 * `failed` with the last error otherwise.
 *
 * The resource runs at plan time, before any phase records a node, so a re-pass
 * can never rewrite a recorded node. `resolve` is synchronous and does no I/O:
 * it validates the bounded args and builds the script and the fixed host
 * commands from literal tokens plus the captured engine path. This module is
 * import-free of the pi runtime, so a Node harness can unit test it.
 */

import { CAMPAIGN_TOKEN, engineCommand } from "./campaign-resource.ts";

export const PLAN_RESOURCE = "sliceme.plan";
export const PLAN_RESOURCE_VERSION = 1;

/** The re-pass bound. The loop stops after this many planner passes. */
export const PLAN_MAX_PASSES = 3;

/** Keys accepted from `args`; every other field is rejected. */
export const PLAN_RESOURCE_FIELDS = new Set(["campaign", "design"]);

export interface PlanCommands {
	status: string;
	ready: string;
}

export interface PlanHostGrant {
	key: string;
	command: string;
}

export type PlanResourceResolution =
	| { script: string; hostCommands: PlanHostGrant[] }
	| { error: string };

/** The two fixed host commands of the resource. */
export function planCommands(engine: string[], campaign?: string): PlanCommands {
	return {
		status: engineCommand(engine, ["status"], campaign),
		ready: engineCommand(engine, ["ready"], campaign),
	};
}

/**
 * The authority grants for one planning run. The loop reads `status` once for
 * the DAG path, then calls `ready` once per pass to validate the new DAG. The
 * runtime forbids a repeated key, so every pass gets its own keyed grant.
 */
export function planGrants(engine: string[], campaign: string | undefined): PlanHostGrant[] {
	const commands = planCommands(engine, campaign);
	const grants: PlanHostGrant[] = [{ key: "status", command: commands.status }];
	for (let index = 0; index < PLAN_MAX_PASSES; index++) {
		grants.push({ key: `ready-${index}`, command: commands.ready });
	}
	return grants;
}

/**
 * The planner re-pass loop, as a pi-subagents workflow script.
 *
 * The sandbox has no file access, so the DAG path and the branch names arrive
 * as JSON from `runs.host`. The planner runs through `runs.run`. A failed
 * `ready` is the plan error, and the next pass receives that text.
 */
export function buildPlanScript(
	commands: PlanCommands,
	design: string,
	maxPasses: number,
): string {
	return [
		`const MAX_PASSES = ${maxPasses};`,
		`const DESIGN = ${JSON.stringify(design)};`,
		`const HOST_TIMEOUT_MS = 3600000;`,
		`const statusRes = await runs.host("status", { kind: "command", command: ${JSON.stringify(commands.status)}, timeoutMs: HOST_TIMEOUT_MS });`,
		`if (!statusRes.ok) throw new Error("sliceme status failed: " + (statusRes.error || statusRes.stderr || statusRes.exitCode));`,
		`const status = JSON.parse(statusRes.stdout);`,
		`const dagPath = status && status.dag_path ? String(status.dag_path) : "";`,
		`if (!dagPath) throw new Error("the engine did not report a DAG path; run sliceme start first");`,
		`const branch = status && status.work_branch ? String(status.work_branch) : "";`,
		`const base = status && status.base_branch ? String(status.base_branch) : "";`,
		`let error = "";`,
		`let pass = 0;`,
		`while (pass < MAX_PASSES) {`,
		`  const task = "Plan the Sliceme campaign from the design document " + JSON.stringify(DESIGN) + ". " +`,
		`    "Write exactly one file with the Write tool: the DAG at " + dagPath + ". " +`,
		`    (branch ? "Set feature_branch to " + JSON.stringify(branch) + ". " : "") +`,
		`    (base ? "Set base to " + JSON.stringify(base) + ". " : "") +`,
		`    "Follow your planner rules: one node per directory, one visit per directory, a slug node id, and an ordered tasks list. " +`,
		`    "Do not commit the DAG." +`,
		`    (error ? " The engine rejected the previous plan. Fix this error: " + error : "");`,
		`  await runs.run("planner-" + pass, { agent: "sliceme-planner", task: task });`,
		`  const readyRes = await runs.host("ready-" + pass, { kind: "command", command: ${JSON.stringify(commands.ready)}, timeoutMs: HOST_TIMEOUT_MS });`,
		`  if (readyRes.ok) {`,
		`    let ready = null;`,
		`    try { ready = JSON.parse(readyRes.stdout); } catch (parseError) { ready = null; }`,
		`    const ids = ready && Array.isArray(ready.ready) ? ready.ready : [];`,
		`    if (ids.length > 0) return { state: "planned", passes: pass + 1, dag: dagPath, ready: ids.length };`,
		`    error = "the plan has no ready node; write at least one node with a directory";`,
		`  } else {`,
		`    error = readyRes.error || readyRes.stderr || readyRes.stdout || "the engine rejected the plan";`,
		`  }`,
		`  pass += 1;`,
		`}`,
		`return { state: "failed", passes: pass, dag: dagPath, error: error };`,
	].join("\n");
}

/**
 * Validate the resource args and expand them into the planning script.
 *
 * Synchronous and bounded: it validates every field and builds each command
 * from fixed literals plus the captured engine path and the optional campaign
 * token. It performs no I/O.
 */
export function resolvePlanResource(
	args: Readonly<Record<string, unknown>>,
	engine: string[],
): PlanResourceResolution {
	const unknown = Object.keys(args).filter((key) => !PLAN_RESOURCE_FIELDS.has(key));
	if (unknown.length) {
		return {
			error: `workflow '${PLAN_RESOURCE}' args contain unsupported fields: ${unknown.join(", ")}.`,
		};
	}
	if (engine.length === 0) {
		return { error: `workflow '${PLAN_RESOURCE}' has no resolved engine path.` };
	}
	let campaign: string | undefined;
	if (args.campaign !== undefined) {
		if (typeof args.campaign !== "string" || !CAMPAIGN_TOKEN.test(args.campaign)) {
			return {
				error: `workflow '${PLAN_RESOURCE}' args.campaign must match [A-Za-z0-9._-]{1,128}.`,
			};
		}
		campaign = args.campaign;
	}
	let design = "DESIGN.md";
	if (args.design !== undefined) {
		if (
			typeof args.design !== "string" ||
			!args.design.trim() ||
			/[\n\r\0]/.test(args.design)
		) {
			return {
				error: `workflow '${PLAN_RESOURCE}' args.design must be a non-empty path without control characters.`,
			};
		}
		design = args.design.trim();
	}
	const commands = planCommands(engine, campaign);
	return {
		script: buildPlanScript(commands, design, PLAN_MAX_PASSES),
		hostCommands: planGrants(engine, campaign),
	};
}
