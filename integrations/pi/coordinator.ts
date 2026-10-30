/**
 * Sliceme pi extension.
 *
 * The extension registers three things and owns no orchestration of its own:
 *
 * 1. The `sliceme` engine tool. It forwards interactive verbs to the bundled
 *    `sliceme` CLI (`surface.ACTIONS`) and returns JSON. The CLI is the engine
 *    surface; the tool adds no scheduler logic.
 * 2. The Sliceme agent definitions (`sliceme-planner`, `sliceme-worker`) through
 *    the installed `pi-subagents` runtime-agent registry.
 * 3. The trusted workflow resource `sliceme.campaign`. The resource holds the
 *    campaign loop; `pi-subagents` owns child execution, tool scoping, status,
 *    and control.
 *
 * The resource resolves the absolute engine path captured in `session_start`.
 * The extension also keeps the suspend/resume contract: the pause flag
 * (`.sliceme/<branch-key>.control.json`) and the descriptor
 * (`.sliceme/<branch-key>.session.json`).
 *
 * Install as part of the `sliceme` pi package; shared helpers live in
 * `./common.ts`.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import {
	CAMPAIGN_RESOURCE,
	CAMPAIGN_RESOURCE_VERSION,
	resolveCampaignResource,
} from "./campaign-resource.ts";
import type { BudgetSpend } from "./budget.ts";
import {
	budgetWindowChanged,
	evaluateBudget,
	hasCampaignBudget,
	nextActiveSeconds,
	readBudgetCaps,
	sessionCostUsd,
	spentUsd,
} from "./budget.ts";
import {
	PLAN_RESOURCE,
	PLAN_RESOURCE_VERSION,
	resolvePlanResource,
} from "./plan-resource.ts";
import {
	applyEngineReply,
	budgetPath,
	clearActiveCampaign,
	controlPath,
	loadPiSubagents,
	packageDir,
	readActiveCampaign,
	readJson,
	resolveSlicemeInvocation,
	runSliceme,
	sessionPath,
	stateDir,
	writeJson,
} from "./common.ts";

const PAUSE_TTL_SECONDS = 3600;

/** The campaign budget watchdog period, in milliseconds. */
const BUDGET_TICK_MS = 15_000;

/** The active-seconds clamp: two ticks, so a crash gap cannot consume the cap. */
const BUDGET_MAX_DELTA_SECONDS = (BUDGET_TICK_MS / 1000) * 2;

/** Engine verbs that call for a human decision or report a campaign. */
const ENGINE_ACTIONS = [
	"start",
	"budget",
	"status",
	"sessions",
	"resume",
	"gc",
	"doctor",
	"ready",
	"plan",
	"open",
	"record",
	"check",
	"report",
	"evidence",
	"deliver",
] as const;

type EngineAction = (typeof ENGINE_ACTIONS)[number];

/** Parameter names each engine verb forwards to the CLI. */
const ACTION_FLAGS: Record<EngineAction, readonly string[]> = {
	start: [
		"path",
		"design",
		"feature_branch",
		"base",
		"mode",
		"checks",
		"budget_seconds",
		"budget_usd",
		"force",
		"campaign",
	],
	budget: ["seconds", "usd", "clear", "campaign"],
	status: ["unit", "short", "dense", "verbose", "campaign"],
	sessions: [],
	resume: ["campaign"],
	gc: [],
	doctor: [],
	ready: ["campaign", "brief"],
	plan: ["design", "campaign"],
	open: ["campaign"],
	record: ["reopen", "campaign"],
	check: ["phase", "refresh", "campaign"],
	report: ["narrative", "design", "campaign"],
	evidence: ["design", "campaign"],
	deliver: ["source", "mode", "confirm", "actor", "note", "cleanup", "no_checks", "campaign"],
};

/** Render one engine verb plus its selected params as CLI arguments. */
function engineArgs(action: string, keys: readonly string[], params: any): string[] {
	const args = [action];
	for (const key of keys) {
		const value = params[key];
		if (value === undefined || value === null) continue;
		const flag = `--${key.replace(/_/g, "-")}`;
		if (typeof value === "boolean") {
			if (value) args.push(flag);
		} else if (Array.isArray(value)) {
			for (const item of value) args.push(flag, String(item));
		} else {
			args.push(flag, String(value));
		}
	}
	return args;
}

// ---------------------------------------------------------------------------
// Agent definitions
// ---------------------------------------------------------------------------

const AGENT_REGISTER_EVENT = "pi-subagents:runtime-agent-register:v1";

interface AgentDefinition {
	description: string;
	systemPrompt: string;
	tools: string[];
}

/** Read one packaged agent definition and its frontmatter `tools:` allowlist. */
function readAgentDefinition(name: string): AgentDefinition | undefined {
	const file = path.join(packageDir(), "integrations", "pi", "agents", `${name}.md`);
	let raw: string;
	try {
		raw = fs.readFileSync(file, "utf8");
	} catch {
		return undefined;
	}
	const front = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(raw);
	const body = front ? raw.slice(front[0].length).trim() : raw.trim();
	const field = (key: string): string | undefined => {
		if (!front) return undefined;
		for (const line of front[1].split(/\r?\n/)) {
			const idx = line.indexOf(":");
			if (idx === -1) continue;
			if (line.slice(0, idx).trim() === key) {
				return line
					.slice(idx + 1)
					.trim()
					.replace(/^"|"$/g, "");
			}
		}
		return undefined;
	};
	const tools = (field("tools") ?? "")
		.split(",")
		.map((tool) => tool.trim())
		.filter(Boolean);
	return { description: field("description") ?? name, systemPrompt: body, tools };
}

/**
 * Register one agent with the installed `pi-subagents` owner.
 *
 * The runtime-agent registry is keyed by the extension's own `pi` object, so an
 * independently installed extension must use the process-local event contract.
 * A missing owner returns `undefined`; the campaign workflow then reports the
 * missing agent at launch.
 */
function registerAgentViaEvents(
	pi: ExtensionAPI,
	name: string,
	definition: AgentDefinition,
): { dispose(): void } | undefined {
	const request: any = {
		version: 1,
		name,
		definition: {
			description: definition.description,
			systemPrompt: definition.systemPrompt,
			tools: definition.tools,
		},
	};
	pi.events.emit(AGENT_REGISTER_EVENT, request);
	const result = request.result;
	if (!result) return undefined;
	if (!result.ok) throw result.error;
	return result.registration;
}

// ---------------------------------------------------------------------------
// Session suspend / resume (`docs/sessions.md`)
// ---------------------------------------------------------------------------

function isPaused(ctx: ExtensionContext, branch: string): boolean {
	const control = readJson<any>(controlPath(ctx.cwd, branch), null);
	if (!control?.pause) return false;
	// A budget stop does not expire. The user must raise the cap and resume.
	if (control.reason === "budget") return true;
	const requested = Number(control.requested_at ?? 0);
	if (requested && Date.now() / 1000 - requested > PAUSE_TTL_SECONDS) return false;
	return true;
}

function clearPause(ctx: ExtensionContext, branch: string): void {
	try {
		fs.rmSync(controlPath(ctx.cwd, branch), { force: true });
	} catch {
		/* best-effort */
	}
}

/** The parsed `.sliceme/config.json` for the plane. */
function readPlaneConfig(cwd: string): any {
	return readJson<any>(path.join(stateDir(cwd), "config.json"), null);
}

/** The adapter's spend file, or an empty object. */
function readSpend(cwd: string, branch: string): BudgetSpend {
	return readJson<BudgetSpend>(budgetPath(cwd, branch), {});
}

/** Clear a budget pause flag, because a new cap window opened. */
function clearBudgetPause(ctx: ExtensionContext, branch: string): void {
	const control = readJson<any>(controlPath(ctx.cwd, branch), null);
	if (control?.reason === "budget") clearPause(ctx, branch);
}

/**
 * Add one tick of active time, record the spend, and optionally trip the caps.
 *
 * The extension is the one writer of the spend file and the pause flag. The
 * engine reads the caps from `config.json` and writes neither file
 * (`docs/sessions.md` §4). This function never aborts the running phase: the
 * campaign resource stops at its next `ready` call, which is the safe point.
 */
function updateBudget(ctx: ExtensionContext, branch: string): void {
	const config = readPlaneConfig(ctx.cwd);
	const caps = readBudgetCaps(config);
	if (!hasCampaignBudget(caps)) return;
	const nowSeconds = Date.now() / 1000;
	const declaredWindow = Number(config?.budget_started_at ?? 0);
	let spend = readSpend(ctx.cwd, branch);
	if (Number(spend.window_started_at ?? 0) !== declaredWindow) {
		// A `budget` action opened a new window: the counters start at zero.
		spend = { window_started_at: declaredWindow, active_seconds: 0 };
	}
	const sessionCost = sessionCostUsd(ctx.sessionManager.getEntries());
	const baseline =
		typeof spend.session_baseline_usd === "number" &&
		Number.isFinite(spend.session_baseline_usd)
			? spend.session_baseline_usd
			: sessionCost;
	const withBaseline: BudgetSpend = { ...spend, session_baseline_usd: baseline };
	const active = nextActiveSeconds(
		spend,
		nowSeconds,
		BUDGET_MAX_DELTA_SECONDS,
		declaredWindow > 0 ? declaredWindow : undefined,
	);
	const spent = spentUsd(withBaseline, sessionCost);
	const verdict = evaluateBudget({ caps, activeSeconds: active, spentUsd: spent });
	writeJson(budgetPath(ctx.cwd, branch), {
		window_started_at: declaredWindow,
		active_seconds: active,
		last_tick_at: nowSeconds,
		spent_usd: spent,
		session_baseline_usd: baseline,
	});
	if (!verdict.tripped) return;
	const existingControl = readJson<any>(controlPath(ctx.cwd, branch), null);
	if (existingControl?.pause) return;
	writeJson(controlPath(ctx.cwd, branch), {
		pause: true,
		reason: "budget",
		metric: verdict.metric,
		requested_at: nowSeconds,
	});
	// Mark the descriptor now, so a shutdown or a crash cannot lose the stop
	// reason. `writeSessionDescriptor` also creates the file when it is missing.
	writeSessionDescriptor(ctx, branch, {
		status: "suspended",
		reason: "budget",
		extra: { budget: { metric: verdict.metric, caps } },
	});
	if (ctx.hasUI) {
		const label = verdict.metric === "cost_usd" ? "cost" : "wall clock";
		ctx.ui.notify(
			`sliceme: the campaign ${label} budget is spent; the campaign stops after ` +
				`the current phase. Raise it with the "budget" action, then resume.`,
			"warning",
		);
	}
}

/**
 * The engine's `config.json` mirror of the campaign's work branch.
 *
 * The per-process pointer is the primary source; this mirror is the fallback
 * for a resumed session in a new process (`docs/multi-campaign.md` §4.4).
 */
function configuredBranch(cwd: string): string | undefined {
	const config = readPlaneConfig(cwd);
	const branch = config?.work_branch ?? config?.target_branch ?? config?.main_branch;
	return typeof branch === "string" && branch ? branch : undefined;
}

interface SessionDescriptorOptions {
	status?: string;
	reason?: string;
	label?: string;
	campaign?: string;
	plan?: any;
	/** Extra descriptor fields, merged last (for example the budget stop). */
	extra?: Record<string, unknown>;
}

/** Write the pi-session descriptor for *branch* (`.sliceme/<key>.session.json`). */
function writeSessionDescriptor(
	ctx: ExtensionContext,
	branch: string,
	over: SessionDescriptorOptions = {},
): any {
	const plan = over.plan ?? {};
	const existing = readJson<any>(sessionPath(ctx.cwd, branch), undefined) ?? {};
	let sessionId: string | undefined;
	let sessionFile: string | undefined;
	try {
		sessionId = ctx.sessionManager.getSessionId();
		sessionFile = ctx.sessionManager.getSessionFile();
	} catch {
		/* ephemeral (--no-session) runs have no session manager entry */
	}
	const descriptor = {
		campaign: plan.campaign ?? over.campaign ?? existing.campaign ?? null,
		feature_branch: branch,
		work_branch: plan.work_branch ?? existing.work_branch ?? null,
		pi: { session_id: sessionId, session_file: sessionFile, cwd: ctx.cwd },
		label: over.label ?? existing.label,
		status: over.status ?? "suspended",
		reason: over.reason ?? "user",
		suspended_at: Date.now() / 1000,
		current_phase: plan.current_phase ?? existing.current_phase ?? null,
		nodes: plan.nodes ?? existing.nodes ?? {},
		resume_plan: plan.resume_plan ?? existing.resume_plan ?? {},
		...(over.extra ?? {}),
	};
	writeJson(sessionPath(ctx.cwd, branch), descriptor);
	return descriptor;
}

/** Whether a node status counts as finished for suspend/resume reporting. */
function isFinishedStatus(status: string): boolean {
	return status === "recorded";
}

/** Whether the engine's resume plan still has campaign work to do. */
function planHasWork(plan: any): boolean {
	if (!plan) return true;
	const nodes = (plan.nodes ?? {}) as Record<string, string>;
	const statuses = Object.values(nodes);
	if (statuses.some((status) => !isFinishedStatus(status))) return true;
	const rp = plan.resume_plan ?? {};
	// A recorded node re-verifies from the check cache, so its resume-plan entry
	// is not new work.
	const pending = (["resume", "respawn", "verify"] as const).some((key) =>
		((rp[key] ?? []) as string[]).some((id) => !isFinishedStatus(nodes[id] ?? "pending")),
	);
	return (
		pending || (rp.record_phase !== undefined && rp.record_phase !== null)
	);
}

/**
 * The shutdown descriptor status (`docs/sessions.md` §3).
 *
 * A delivered campaign is `completed`; a campaign with every phase finished but
 * no delivery is `ready`; anything else is `suspended`.  A recorded node is a
 * finished node, because only delivery remains.  Shutdown runs no subprocess,
 * so the last recorded descriptor supplies the phase progress.
 */
function shutdownStatus(ctx: ExtensionContext, branch: string): string {
	const descriptor = readJson<any>(sessionPath(ctx.cwd, branch), undefined);
	if (!descriptor) return "suspended";
	if (descriptor.status === "completed") return "completed";
	const statuses = Object.values(descriptor.nodes ?? {}) as string[];
	const finished = statuses.length > 0 && statuses.every(isFinishedStatus);
	if (finished) return "ready";
	return "suspended";
}

/** A resume prompt that reports progress and points at the campaign resource. */
function resumePrompt(branch: string, descriptor: any, plan: any): string {
	const statuses = (plan?.nodes ?? {}) as Record<string, string>;
	const values = Object.values(statuses);
	const done = values.filter(isFinishedStatus).length;
	const lines = [
		`Resume the suspended Sliceme campaign "${descriptor?.campaign ?? branch}".`,
		`Progress: phase ${plan?.current_phase ?? "?"} (${done}/${values.length || "?"} nodes done).`,
	];
	for (const [node, status] of Object.entries(statuses)) {
		lines.push(`- ${node}: ${status}`);
	}
	lines.push(
		`Resume plan: ${JSON.stringify(plan?.resume_plan ?? {})}.`,
		"Continue by launching the campaign workflow: call the subagent tool with " +
			`workflow "${CAMPAIGN_RESOURCE}" and async true. Do not restart finished work.`,
	);
	return lines.join("\n");
}

// ---------------------------------------------------------------------------
// Extension
// ---------------------------------------------------------------------------

export default function coordinatorExtension(pi: ExtensionAPI) {
	/** The absolute engine invocation, captured once per session. */
	let engine: string[] = [];
	let resourceRegistration: { dispose(): void } | undefined;
	let planResourceRegistration: { dispose(): void } | undefined;
	let agentRegistrations: { dispose(): void }[] = [];

	const sliceme = (ctx: ExtensionContext, args: string[], signal?: AbortSignal) =>
		runSliceme(pi, ctx, args, signal);

	const activeCampaign = (cwd: string): string | undefined =>
		readActiveCampaign(cwd) ?? configuredBranch(cwd);

	let budgetTimer: ReturnType<typeof setInterval> | undefined;

	/** Stop the budget watchdog (shutdown, or a replaced session). */
	function stopBudgetWatchdog(): void {
		if (budgetTimer) clearInterval(budgetTimer);
		budgetTimer = undefined;
	}

	/**
	 * Start the campaign budget watchdog.
	 *
	 * One tick adds one active-time delta, records the spend, and writes the
	 * pause flag when a cap trips. The tick never aborts a turn: the campaign
	 * resource stops at its next `ready` call, which is the phase boundary.
	 */
	function startBudgetWatchdog(ctx: ExtensionContext): void {
		stopBudgetWatchdog();
		budgetTimer = setInterval(() => {
			try {
				const branch = activeCampaign(ctx.cwd);
				if (!branch) return;
				const descriptor = readJson<any>(sessionPath(ctx.cwd, branch), undefined);
				if (descriptor?.status === "completed" || descriptor?.status === "ready") return;
				const control = readJson<any>(controlPath(ctx.cwd, branch), null);
				if (control?.pause) return;
				updateBudget(ctx, branch);
			} catch {
				/* a watchdog tick must never break the session */
			}
		}, BUDGET_TICK_MS);
		(budgetTimer as any)?.unref?.();
	}

	/** Make the engine tool callable for this session. */
	function activateTool(): void {
		const active = new Set(pi.getActiveTools());
		active.add("sliceme");
		pi.setActiveTools([...active]);
	}

	/**
	 * Capture the absolute engine command. The resource `resolve` function reads
	 * this closure value and never performs I/O or a relative-path lookup.
	 */
	function captureEngine(): void {
		const invocation = resolveSlicemeInvocation();
		const command =
			invocation.command.includes("/") || invocation.command.includes("\\")
				? path.resolve(invocation.command)
				: invocation.command;
		engine = [command, ...invocation.prefix.map((entry) => path.resolve(entry))];
	}

	/** Register the two Sliceme agents with the installed pi-subagents owner. */
	function registerAgents(): void {
		for (const registration of agentRegistrations) registration.dispose();
		agentRegistrations = [];
		for (const [name, file] of [
			["sliceme-planner", "planner"],
			["sliceme-worker", "worker"],
		] as const) {
			const definition = readAgentDefinition(file);
			if (!definition) continue;
			const registration = registerAgentViaEvents(pi, name, definition);
			if (registration) agentRegistrations.push(registration);
		}
	}

	/** Register the trusted campaign and planning workflow resources. */
	async function registerWorkflowResources(ctx: ExtensionContext): Promise<void> {
		const mod = await loadPiSubagents("workflow-resources");
		if (!mod?.registerWorkflowResource) return;
		const sessionId = ctx.sessionManager.getSessionId();
		resourceRegistration?.dispose();
		planResourceRegistration?.dispose();
		resourceRegistration = mod.registerWorkflowResource({
			sessionId,
			definition: {
				name: CAMPAIGN_RESOURCE,
				version: CAMPAIGN_RESOURCE_VERSION,
				resolve: (args: Readonly<Record<string, unknown>>) => resolveCampaignResource(args, engine),
			},
		});
		planResourceRegistration = mod.registerWorkflowResource({
			sessionId,
			definition: {
				name: PLAN_RESOURCE,
				version: PLAN_RESOURCE_VERSION,
				resolve: (args: Readonly<Record<string, unknown>>) => resolvePlanResource(args, engine),
			},
		});
	}

	/** The engine's resume plan; git plus SQLite win over the descriptor. */
	async function fetchResumePlan(ctx: ExtensionContext, branch: string): Promise<any | null> {
		try {
			return (
				await sliceme(ctx, ["resume", "--campaign", branch])
			).json;
		} catch {
			return null;
		}
	}

	pi.registerTool({
		name: "sliceme",
		label: "Sliceme",
		description:
			"Drive a Sliceme campaign through the engine: start (plane + campaign), plan " +
			"(design split), status (campaign state), sessions (registered campaigns), resume " +
			"(resume plan), gc (prune), doctor (health), ready " +
			"(current-phase node ids and paused), open (create the campaign worktree), record " +
			"(the current phase as per-node commits), check (combined-tree checks), budget (set, " +
			"raise, or clear the campaign caps), report (the deterministic report), evidence (the " +
			"deterministic evidence document), and deliver (confirm the one approval, then push + " +
			"pull request, or a local merge). The planning loop runs in " +
			"the sliceme.plan workflow resource, and the campaign loop runs in the " +
			"sliceme.campaign workflow resource.",
		promptSnippet: "Call the Sliceme engine (start → plan → resource loop → deliver)",
		promptGuidelines: [
			"Start a campaign with `start` and inspect the design split with `plan`. Then " +
				"run the planning loop through the `sliceme.plan` workflow resource: call the " +
				'subagent tool with workflow "sliceme.plan", async false, and args {design}. ' +
				"If it returns state `failed`, stop and report the error. Then run `open` to " +
				"create the campaign worktree from the base branch, and run the campaign loop " +
				"through the `sliceme.campaign` workflow resource: call the subagent tool with " +
				'workflow "sliceme.campaign" and async true.',
			"The resource reads the current phase from the engine. Do not pass node ids or phase " +
				"indices to it; the engine reads its own state.",
			"Workers are pure editors in the shared campaign worktree; they never run git and " +
				"never run the test suite. The engine records per-node commits and runs one " +
				"combined-tree check.",
			"Stop at the human gates: an explicit user suspension and the campaign " +
				"confirmation before `deliver`.",
			"`ready` reports `paused`, `pause_reason`, and `budget`. A `budget` pause " +
				"needs the `budget` action to raise the cap before a resume; do not resume a " +
				"budget-stopped campaign without it.",
			"After the campaign resource returns `complete`, run `evidence`, then " +
				"run `report`. A `capped` return is not complete: run the resource again before " +
				"`evidence`. Show the evidence summary and the delivery details to the user: " +
				"for the `pull_request` mode the head branch, the base `main`, and the title; " +
				"for the `local` mode the base branch and the coming local merge. Ask for " +
				"confirmation one time. On a confirmation run `deliver` with `confirm: true` and the " +
				"selected `mode`; on a decline stop and keep the campaign for a later attempt.",
			"The delivery mode is `pull_request` (push the branch and open a pull request) " +
				"or `local` (merge the campaign branch into the local base branch and stop, " +
				"so the user pushes later). The plane default comes from `start` with `mode`; " +
				"`deliver` accepts `mode` to override it for one campaign.",
			"A node is ready only once every dependency is done. Use `ready` for the ready node " +
				"ids, the current phase, and `paused`.",
		],
		// Inactive until `/sliceme` activates it, so a plain session never
		// advertises the campaign.
		defaultActive: false,
		parameters: Type.Object({
			action: StringEnum(ENGINE_ACTIONS),
			path: Type.Optional(Type.String({ description: "start: directory to bootstrap" })),
			design: Type.Optional(
				Type.String({ description: "start/plan/report/evidence: design path" }),
			),
			campaign: Type.Optional(Type.String({ description: "campaign to operate on" })),
			base: Type.Optional(Type.String({ description: "start: delivery base override" })),
			mode: Type.Optional(
				StringEnum(["pull_request", "local"] as const, {
					description: "start/deliver: pull_request (push + PR) or local (merge the local base)",
				}),
			),
			feature_branch: Type.Optional(
				Type.String({ description: "start: campaign branch (pull request head)" }),
			),
			checks: Type.Optional(
				Type.Array(Type.String(), { description: "start: trusted check NAME=COMMAND" }),
			),
			budget_seconds: Type.Optional(
				Type.Number({ description: "start: wall-clock cap in seconds" }),
			),
			budget_usd: Type.Optional(Type.Number({ description: "start: cost cap in USD" })),
			seconds: Type.Optional(Type.Number({ description: "budget: wall-clock cap in seconds" })),
			usd: Type.Optional(Type.Number({ description: "budget: cost cap in USD" })),
			clear: Type.Optional(Type.Boolean({ description: "budget: clear both caps" })),
			force: Type.Optional(Type.Boolean({ description: "start: overwrite an existing config" })),
			unit: Type.Optional(Type.String({ description: "status: show one unit" })),
			short: Type.Optional(Type.Boolean({ description: "status: print only the unit name" })),
			dense: Type.Optional(Type.Boolean({ description: "status: print the compact summary" })),
			verbose: Type.Optional(Type.Boolean({ description: "status: print the full dump" })),
			confirm: Type.Optional(
				Type.Boolean({ description: "deliver: confirm the one campaign approval" }),
			),
			no_checks: Type.Optional(
				Type.Boolean({ description: "deliver: skip the trusted checks" }),
			),
			phase: Type.Optional(
				Type.Number({ description: "check: re-run the checks of one recorded phase" }),
			),
			refresh: Type.Optional(
				Type.Boolean({ description: "check: bypass the verdict cache" }),
			),
			reopen: Type.Optional(
				Type.Number({
					description:
						"record: drop the records at or after this phase, reset the worktree, and leave the cursor there",
				}),
			),
			narrative: Type.Optional(Type.String({ description: "report: what-changed text" })),
			note: Type.Optional(Type.String({ description: "deliver: approval note" })),
			actor: Type.Optional(Type.String({ description: "deliver: who confirmed the delivery" })),
			source: Type.Optional(Type.String({ description: "deliver: worktree branch" })),
			cleanup: Type.Optional(
				StringEnum(["none", "worktrees", "all"] as const, {
					description: "deliver: post-merge cleanup",
				}),
			),
		}),
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const action = params.action as EngineAction;
			const flags = ACTION_FLAGS[action] ?? [];
			const { json, text } = await sliceme(ctx, engineArgs(action, flags, params), signal);
			// Update the active pointer and mark a successful delivery completed.
			const branch = applyEngineReply(ctx.cwd, action, json);
			// A raised cap clears the budget pause, so the campaign can continue.
			if (action === "budget") {
				const target = branch ?? activeCampaign(ctx.cwd);
				if (target) clearBudgetPause(ctx, target);
			}
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});

	pi.registerCommand("sliceme", {
		description: "Start a Sliceme campaign from a design document (default DESIGN.md)",
		handler: async (args, ctx) => {
			const design = args.trim() || "DESIGN.md";
			if (!ctx.isIdle()) {
				ctx.ui.notify("sliceme: the agent is busy; finish the current turn first.", "warning");
				return;
			}
			activateTool();
			ctx.ui.notify(`sliceme: starting a campaign from ${design}`, "info");
			pi.sendUserMessage(
				`Start a Sliceme campaign for the design document "${design}". ` +
					`Use the sliceme tool with action "start", passing design "${design}", ` +
					`and then "plan". Run the planning loop through the sliceme.plan workflow ` +
					`resource: call the subagent tool with workflow "sliceme.plan", async false, ` +
					`and args {design: "${design}"}. If it returns state failed, stop and report ` +
					`the error. Run "open" to create the campaign worktree ` +
					`from the base branch. Run the campaign ` +
					`loop through the sliceme.campaign workflow resource: call the subagent tool ` +
					`with workflow "sliceme.campaign" and async true. If it returns state capped, ` +
					`run the same resource again; a capped run is not a failure. When it completes, run ` +
					`"evidence" and "report", show the evidence summary and the delivery ` +
					`details to the user, and ask for confirmation one time. Ask which delivery ` +
					`mode to use: "pull_request" (push the branch and open a pull request) or ` +
					`"local" (merge the local base branch, then push later). On a ` +
					`confirmation run "deliver" with "confirm": true and the chosen "mode"; on a ` +
					`decline stop and keep the campaign. Stop only at the human gates: an ` +
					`explicit user suspension and the final confirmation before delivery.`,
			);
		},
	});

	pi.registerCommand("suspend", {
		description: "Suspend the current Sliceme campaign and register it for resume",
		handler: async (args, ctx) => {
			const branch = activeCampaign(ctx.cwd);
			if (!branch) {
				ctx.ui.notify("sliceme: no campaign in this directory", "warning");
				return;
			}
			const label = args.trim() || undefined;
			writeJson(controlPath(ctx.cwd, branch), {
				pause: true,
				requested_at: Date.now() / 1000,
				label,
			});
			// Abort the in-flight turn so suspension lands at the next safe point.
			if (!ctx.isIdle()) {
				ctx.abort();
				await ctx.waitForIdle();
			}
			clearPause(ctx, branch);
			const plan = await fetchResumePlan(ctx, branch);
			const descriptor = writeSessionDescriptor(ctx, branch, {
				status: "suspended",
				reason: "user",
				label,
				plan,
			});
			pi.appendEntry("sliceme.session", {
				campaign: descriptor.campaign,
				feature_branch: branch,
				status: descriptor.status,
				resume_plan: descriptor.resume_plan,
			});
			if (ctx.hasUI) {
				ctx.ui.notify(
					`sliceme: suspended '${descriptor.campaign ?? branch}'; ` +
						`resume with pi --continue, /resume, or /campaigns`,
					"info",
				);
			}
		},
	});

	pi.registerCommand("campaigns", {
		description: "List suspended Sliceme campaigns and resume one",
		handler: async (_args, ctx) => {
			let sessions: any[] = [];
			try {
				sessions = (await sliceme(ctx, ["sessions"])).json?.sessions ?? [];
			} catch (error) {
				ctx.ui.notify(`sliceme: ${String((error as Error)?.message ?? error)}`, "error");
				return;
			}
			if (!sessions.length) {
				ctx.ui.notify("sliceme: no registered campaigns", "info");
				return;
			}
			if (!ctx.hasUI) return;
			const labels = sessions.map(
				(s) =>
					`${s.label ?? s.work_branch} — phase ${s.phase ?? "?"} ` +
					`${s.done}/${s.total} ${s.status}`,
			);
			const choice = await ctx.ui.select("Sliceme campaigns", labels);
			if (!choice) return;
			const selected = sessions[labels.indexOf(choice)];
			if (!selected?.session_file) {
				ctx.ui.notify("sliceme: no pi session file recorded for that campaign", "warning");
				return;
			}
			if (selected.is_current) {
				ctx.ui.notify("sliceme: that campaign is already the current session", "info");
				return;
			}
			await ctx.switchSession(selected.session_file);
		},
	});

	pi.on("session_start", async (event, ctx) => {
		captureEngine();
		startBudgetWatchdog(ctx);
		try {
			registerAgents();
			await registerWorkflowResources(ctx);
		} catch (error) {
			if (ctx.hasUI) {
				ctx.ui.notify(
					`sliceme: could not register the workflow resources: ${String((error as Error)?.message ?? error)}`,
					"warning",
				);
			}
		}
		try {
			if (event.reason !== "resume" && event.reason !== "startup") return;
			const branch = activeCampaign(ctx.cwd);
			if (!branch) return;
			const descriptor = readJson<any>(sessionPath(ctx.cwd, branch), undefined);
			if (!descriptor || descriptor.status !== "suspended") return;
			if (isPaused(ctx, branch)) {
				const control = readJson<any>(controlPath(ctx.cwd, branch), null);
				const raised =
					control?.reason === "budget" &&
					budgetWindowChanged(readPlaneConfig(ctx.cwd), readSpend(ctx.cwd, branch));
				if (!raised) {
					if (ctx.hasUI && control?.reason === "budget") {
						ctx.ui.notify(
							`sliceme: the budget cap stopped this campaign; raise it with the ` +
								`"budget" action, then resume.`,
							"warning",
						);
					}
					return;
				}
				clearPause(ctx, branch);
			}
			const plan = await fetchResumePlan(ctx, branch);
			if (!planHasWork(plan)) {
				clearPause(ctx, branch);
				writeJson(sessionPath(ctx.cwd, branch), {
					...descriptor,
					status: "ready",
					completed_at: Date.now() / 1000,
				});
				return;
			}
			clearPause(ctx, branch);
			const text = resumePrompt(branch, descriptor, plan);
			// Mark it active so a reload/restart does not inject the prompt twice.
			writeJson(sessionPath(ctx.cwd, branch), {
				...descriptor,
				status: "active",
				resumed_at: Date.now() / 1000,
			});
			if (event.reason === "resume") {
				activateTool();
				pi.sendUserMessage(text);
				return;
			}
			if (!ctx.hasUI || !ctx.isIdle()) return;
			const ok = await ctx.ui.confirm("Resume Sliceme campaign?", text);
			if (ok) {
				activateTool();
				pi.sendUserMessage(text);
			}
		} catch {
			/* a resume hook must never break session startup */
		}
	});

	pi.on("session_shutdown", (_event, ctx) => {
		stopBudgetWatchdog();
		for (const registration of agentRegistrations) registration.dispose();
		agentRegistrations = [];
		resourceRegistration?.dispose();
		resourceRegistration = undefined;
		planResourceRegistration?.dispose();
		planResourceRegistration = undefined;
		try {
			const branch = activeCampaign(ctx.cwd);
			clearActiveCampaign(ctx.cwd);
			if (!branch) return;
			const pending = readJson<any>(controlPath(ctx.cwd, branch), null);
			const reason = pending?.pause && pending.reason === "budget" ? "budget" : "user";
			writeSessionDescriptor(ctx, branch, {
				status: shutdownStatus(ctx, branch),
				reason,
			});
		} catch {
			/* never block shutdown */
		}
	});
}
