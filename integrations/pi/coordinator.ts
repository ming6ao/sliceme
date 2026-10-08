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
import {
	applyEngineReply,
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

/** Engine verbs that call for a human decision or report a campaign. */
const ENGINE_ACTIONS = [
	"start",
	"status",
	"ready",
	"plan",
	"wave",
	"check",
	"review",
	"deliver",
] as const;

type EngineAction = (typeof ENGINE_ACTIONS)[number];

/** Parameter names each engine verb forwards to the CLI. */
const ACTION_FLAGS: Record<EngineAction, readonly string[]> = {
	start: [
		"name",
		"path",
		"kind",
		"base",
		"target",
		"target_mode",
		"worktree_branch",
		"checks",
		"force",
		"no_unit",
		"campaign",
	],
	status: [
		"unit",
		"short",
		"dense",
		"verbose",
		"simulate",
		"health",
		"gc",
		"no_checks",
		"sessions",
		"resume",
		"plan_only",
		"campaign",
	],
	ready: ["campaign"],
	plan: ["design", "campaign"],
	wave: ["open", "record", "wave", "current", "only", "messages", "summary", "campaign"],
	check: ["current", "campaign"],
	review: ["decision", "all", "report", "narrative", "design", "commit", "note", "actor", "campaign"],
	deliver: ["target", "source", "cleanup", "no_checks", "campaign"],
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

/**
 * The engine's `config.json` mirror of the campaign's feature branch.
 *
 * The per-process pointer is the primary source; this mirror is the fallback
 * for a resumed session in a new process (`docs/multi-campaign.md` §4.4).
 */
function configuredBranch(cwd: string): string | undefined {
	const config = readJson<any>(path.join(stateDir(cwd), "config.json"), null);
	const branch = config?.target_branch ?? config?.main_branch;
	return typeof branch === "string" && branch ? branch : undefined;
}

interface SessionDescriptorOptions {
	status?: string;
	reason?: string;
	label?: string;
	campaign?: string;
	plan?: any;
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
		worktree_branch: plan.worktree_branch ?? existing.worktree_branch ?? null,
		pi: { session_id: sessionId, session_file: sessionFile, cwd: ctx.cwd },
		label: over.label ?? existing.label,
		status: over.status ?? "suspended",
		reason: over.reason ?? "user",
		suspended_at: Date.now() / 1000,
		current_wave: plan.current_wave ?? existing.current_wave ?? null,
		nodes: plan.nodes ?? existing.nodes ?? {},
		resume_plan: plan.resume_plan ?? existing.resume_plan ?? {},
	};
	writeJson(sessionPath(ctx.cwd, branch), descriptor);
	return descriptor;
}

/** Whether a node status counts as finished for suspend/resume reporting. */
function isFinishedStatus(status: string): boolean {
	return status === "done" || status === "recorded";
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
		pending || (rp.record_wave !== undefined && rp.record_wave !== null)
	);
}

/**
 * The shutdown descriptor status (`docs/sessions.md` §3).
 *
 * A delivered campaign is `completed`; a campaign with every wave finished but
 * no delivery is `ready`; anything else is `suspended`.  A recorded node is a
 * finished node, because only delivery remains.  Shutdown runs no subprocess,
 * so the last recorded descriptor supplies the wave progress.
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
		`Progress: wave ${plan?.current_wave ?? "?"} (${done}/${values.length || "?"} nodes done).`,
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
	let agentRegistrations: { dispose(): void }[] = [];

	const sliceme = (ctx: ExtensionContext, args: string[], signal?: AbortSignal) =>
		runSliceme(pi, ctx, args, signal);

	const activeCampaign = (cwd: string): string | undefined =>
		readActiveCampaign(cwd) ?? configuredBranch(cwd);

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

	/** Register the trusted `sliceme.campaign` workflow resource. */
	async function registerCampaignResource(ctx: ExtensionContext): Promise<void> {
		const mod = await loadPiSubagents("workflow-resources");
		if (!mod?.registerWorkflowResource) return;
		resourceRegistration?.dispose();
		resourceRegistration = mod.registerWorkflowResource({
			sessionId: ctx.sessionManager.getSessionId(),
			definition: {
				name: CAMPAIGN_RESOURCE,
				version: CAMPAIGN_RESOURCE_VERSION,
				resolve: (args: Readonly<Record<string, unknown>>) => resolveCampaignResource(args, engine),
			},
		});
	}

	/** The engine's resume plan; git plus SQLite win over the descriptor. */
	async function fetchResumePlan(ctx: ExtensionContext, branch: string): Promise<any | null> {
		try {
			return (
				await sliceme(ctx, ["status", "--resume", "--plan-only", "--campaign", branch])
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
			"(design split), status, ready (current-wave node ids and paused), wave (open or " +
			"record the current wave), check (combined-tree checks), review (one campaign " +
			"decision, or the report), and deliver (push + pull request). The campaign loop " +
			"runs in the sliceme.campaign workflow resource.",
		promptSnippet: "Call the Sliceme engine (start → plan → resource loop → deliver)",
		promptGuidelines: [
			"Start a campaign with `start` and inspect the design split with `plan`. Then run " +
				"the campaign loop through the `sliceme.campaign` workflow resource: call the " +
				'subagent tool with workflow "sliceme.campaign" and async true.',
			"The resource reads the current wave from the engine. Do not pass node ids or wave " +
				"indices to it; the engine reads its own state.",
			"Workers are pure editors in the shared campaign worktree; they never run git and " +
				"never run the test suite. The engine records per-node commits and runs one " +
				"combined-tree check.",
			"Stop at the human gates: the target-branch choice, an explicit user suspension, " +
				"and the campaign approval before `deliver`.",
			"A node is ready only once every dependency is done. Use `ready` for the ready node " +
				"ids, the current wave, and `paused`.",
		],
		// Inactive until `/sliceme` activates it, so a plain session never
		// advertises the campaign.
		defaultActive: false,
		parameters: Type.Object({
			action: StringEnum(ENGINE_ACTIONS),
			name: Type.Optional(Type.String({ description: "start: unit name" })),
			path: Type.Optional(Type.String({ description: "start: directory to bootstrap" })),
			kind: Type.Optional(StringEnum(["worker"] as const, { description: "start: unit kind" })),
			design: Type.Optional(Type.String({ description: "plan/review report: design path" })),
			campaign: Type.Optional(Type.String({ description: "campaign to operate on" })),
			base: Type.Optional(Type.String({ description: "start: base branch/ref" })),
			target: Type.Optional(
				Type.String({ description: "start/deliver: target (feature) branch" }),
			),
			target_mode: Type.Optional(
				StringEnum(["current", "existing", "new"] as const, {
					description: "start: how to resolve the target branch",
				}),
			),
			worktree_branch: Type.Optional(
				Type.String({ description: "start: campaign accumulation branch" }),
			),
			checks: Type.Optional(
				Type.Array(Type.String(), { description: "start: trusted check NAME=COMMAND" }),
			),
			force: Type.Optional(Type.Boolean({ description: "start: overwrite an existing config" })),
			no_unit: Type.Optional(
				Type.Boolean({ description: "start: initialise the plane without a unit" }),
			),
			unit: Type.Optional(Type.String({ description: "status: show one unit" })),
			short: Type.Optional(Type.Boolean({ description: "status: print only the unit name" })),
			dense: Type.Optional(Type.Boolean({ description: "status: print the compact summary" })),
			verbose: Type.Optional(Type.Boolean({ description: "status: print the full dump" })),
			simulate: Type.Optional(
				Type.Boolean({ description: "status: plan waves and verify the combined tree" }),
			),
			health: Type.Optional(Type.Boolean({ description: "status: check git/plane health" })),
			gc: Type.Optional(Type.Boolean({ description: "status: prune worktrees and branches" })),
			no_checks: Type.Optional(
				Type.Boolean({ description: "status/deliver: skip the trusted checks" }),
			),
			sessions: Type.Optional(
				Type.Boolean({ description: "status: list registered campaigns" }),
			),
			resume: Type.Optional(
				Type.Boolean({ description: "status: reconcile a suspended campaign" }),
			),
			plan_only: Type.Optional(
				Type.Boolean({ description: "status --resume: compatibility no-op; resume writes nothing" }),
			),
			open: Type.Optional(
				Type.Boolean({ description: "wave: create or reuse the campaign worktree" }),
			),
			record: Type.Optional(
				Type.Boolean({ description: "wave: record a wave as per-node commits" }),
			),
			wave: Type.Optional(Type.Number({ description: "wave: wave index to record" })),
			current: Type.Optional(
				Type.Boolean({ description: "wave/check: use the engine's current wave" }),
			),
			only: Type.Optional(
				Type.Array(Type.String(), { description: "wave: scope the record to these nodes" }),
			),
			messages: Type.Optional(
				Type.String({ description: "wave: JSON object of node id to description" }),
			),
			summary: Type.Optional(Type.String({ description: "wave: candidate summary" })),
			decision: Type.Optional(
				StringEnum(["approve", "request_changes", "override"] as const, {
					description: "review: record one campaign decision",
				}),
			),
			all: Type.Optional(
				Type.Boolean({ description: "review: with approve, approve the whole campaign" }),
			),
			report: Type.Optional(
				Type.Boolean({ description: "review: write the deterministic report" }),
			),
			narrative: Type.Optional(Type.String({ description: "review report: what-changed text" })),
			commit: Type.Optional(Type.String({ description: "review: commit to approve" })),
			note: Type.Optional(Type.String({ description: "review: decision note" })),
			actor: Type.Optional(Type.String({ description: "review: who recorded the decision" })),
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
			applyEngineReply(ctx.cwd, action, json);
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
					`Use the sliceme tool with action "start" and then "plan". Run the campaign ` +
					`loop through the sliceme.campaign workflow resource: call the subagent tool ` +
					`with workflow "sliceme.campaign" and async true. Stop only at the human ` +
					`gates: the target-branch choice, an explicit user suspension, and the final ` +
					`approval before delivery.`,
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
				sessions = (await sliceme(ctx, ["status", "--sessions"])).json?.sessions ?? [];
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
					`${s.label ?? s.feature_branch} — wave ${s.wave ?? "?"} ` +
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
		try {
			registerAgents();
			await registerCampaignResource(ctx);
		} catch (error) {
			if (ctx.hasUI) {
				ctx.ui.notify(
					`sliceme: could not register the campaign resource: ${String((error as Error)?.message ?? error)}`,
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
			if (isPaused(ctx, branch)) return;
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
		for (const registration of agentRegistrations) registration.dispose();
		agentRegistrations = [];
		resourceRegistration?.dispose();
		resourceRegistration = undefined;
		try {
			const branch = activeCampaign(ctx.cwd);
			clearActiveCampaign(ctx.cwd);
			if (!branch) return;
			writeSessionDescriptor(ctx, branch, {
				status: shutdownStatus(ctx, branch),
				reason: "user",
			});
		} catch {
			/* never block shutdown */
		}
	});
}
