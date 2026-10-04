/**
 * Sliceme **coordinator** extension for pi — the `sliceme` tool.
 *
 * One top-level coordinator session turns a design document into landed work by
 * spawning a planner, one-shot workers, and a read-only verifier, while the
 * engine remains the deterministic isolation/integration layer.  The DAG in
 * `.sliceme/<branch-key>.dag.json` is the only schedule; there are no phases
 * in the scheduler (docs/guide.md).
 *
 * One tool, `sliceme`, wraps the CLI's orchestration verbs:
 *
 *   start <design>   choose target branch + no-unit plane + planner -> dag.json
 *   status           merge `sliceme status --json` with live child state
 *   ready            nodes whose every dependency is done
 *   spawn <node>     launch a one-shot worker (pure editor) in the campaign worktree
 *   record           commit the current wave onto the campaign worktree
 *   verify <node>    verifier on the node's recorded commit; record a verdict
 *   deliver          after every wave: ask approval, then merge to the target branch
 *   report           `sliceme report` plus the coordinator's narrative
 *
 * All waves commit onto one campaign worktree branch; nothing is merged to the
 * target branch until every wave is done and the user approves `deliver`.
 * Workers edit the shared worktree and never run git.  The target branch is
 * never the default branch and there is no override.
 *
 * Workers are scoped to the `sliceme-unit` tool (`unit.ts`) and run inside
 * their unit worktree; `runSubagent` enforces the agent `tools:` allowlist, so
 * a worker never sees this `sliceme` tool.
 *
 * Workers are child processes of the coordinator and are **not detached**: an
 * orchestrator crash kills them, and on resume any `running` node is reset to
 * `pending`.  Only the single executor runs checks (and the GPU broker);
 * verifiers judge the executor's recorded evidence.
 *
 * The tools register inactive; `/sliceme [DESIGN.md]` activates them and nudges
 * the model to start a campaign (defaulting to ``DESIGN.md``), and the
 * `session_start` hook re-activates them when a suspended campaign is resumed
 * (pi does not restore the active set from the transcript on resume).  There is
 * no separate skill.
 *
 * Install as part of the `sliceme` pi package (`pi install ./` or
 * `pi install npm:sliceme`); shared helpers live in `./common.ts`.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { AgentToolUpdateCallback } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import {
	branchKey,
	CampaignStateStore,
	controlPath,
	dagPath,
	heartbeatPath,
	logEvent,
	logPath,
	readJson,
	renderAgentLine,
	renderProgress,
	reviewLogPath,
	reviewUrlPath,
	runSliceme,
	runSubagent,
	sessionPath,
	spawnReviewServer,
	stateDir,
	statePath,
	writeJson,
} from "./common.ts";
import type {
	ProgressAgent,
	ProgressSnapshot,
	ReviewServerHandle,
	SubagentProgress,
	SubagentResult,
} from "./common.ts";

export const CAMPAIGN_ACTIONS = [
	"start",
	"status",
	"ready",
	"spawn",
	"record",
	"verify",
	"deliver",
	"report",
	"exec",
	"wave",
	"review",
] as const;

/** Parameter names the `review` action forwards to the engine verb. */
const REVIEW_KEYS = [
	"poll",
	"ack",
	"state",
	"diff",
	"comment",
	"decision",
	"all",
	"report",
	"narrative",
	"design",
	"comment_id",
	"target",
	"commit",
	"file",
	"side",
	"line",
	"line_end",
	"body",
	"note",
	"actor",
] as const;

/** Parameter names the `exec` action forwards to the engine verb. */
const EXEC_KEYS = [
	"validate",
	"gpu_required",
	"run",
	"submit",
	"wait",
	"cancel",
	"job",
	"source",
	"commit",
	"command",
	"sandbox",
	"gpu",
	"priority",
	"timeout",
	"wave",
	"requester",
	"limit",
] as const;

/** Parameter names the `wave` action forwards to the engine verb. */
const WAVE_KEYS = ["open", "record", "wave", "messages", "summary"] as const;

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

interface CampaignNode {
	id: string;
	label?: string;
	phase?: string;
	goal?: string;
	owns?: string[];
	depends_on?: string[];
	acceptance?: string[];
	gpu?: "none" | "T1" | "T2";
}

interface Dag {
	campaign?: string;
	feature_branch?: string;
	base?: string;
	design?: string;
	concurrency?: number;
	max_attempts?: number;
	nodes?: CampaignNode[];
}

interface NodeState {
	status: "pending" | "running" | "paused" | "recorded" | "done" | "failed" | "stopped";
	attempts?: number;
	verdict?: string;
	lastError?: string;
	description?: string;
	unit?: string;
	branch?: string;
	worktree?: string;
	commit?: string;
	candidate?: number;
	wave?: number;
}

interface WaveState {
	index: number;
	members: string[];
	status: "pending" | "running" | "done";
	integrated: string[];
	cleanup_done: boolean;
}

interface CampaignState {
	campaign?: string;
	feature_branch?: string;
	target_branch?: string;
	worktree_branch?: string;
	base?: string;
	delivered?: boolean;
	wave_size?: number;
	current_wave?: number;
	dag_fingerprint?: string;
	waves?: WaveState[];
	nodes: Record<string, NodeState>;
}

function nodeIds(dag: Dag): string[] {
	return (dag.nodes ?? []).map((n) => n.id);
}

function nodeStatus(state: CampaignState, id: string): string {
	return state.nodes?.[id]?.status ?? "pending";
}

/** Seconds after which a leftover pause flag is ignored on resume. */
const PAUSE_TTL_SECONDS = 3600;

/** The campaign branch recorded in an existing plane's config, without a subprocess. */
function configuredBranch(cwd: string): string | undefined {
	const cfg = readJson<any>(path.join(stateDir(cwd), "config.json"), undefined);
	return cfg?.target_branch ?? cfg?.main_branch;
}

function readyNodes(dag: Dag, state: CampaignState): string[] {
	const done = new Set(nodeIds(dag).filter((id) => nodeStatus(state, id) === "done"));
	return nodeIds(dag).filter((id) => {
		if (nodeStatus(state, id) === "done" || nodeStatus(state, id) === "running") return false;
		const node = (dag.nodes ?? []).find((n) => n.id === id);
		return (node?.depends_on ?? []).every((dep) => done.has(dep));
	});
}

/** Structural identity of the DAG inputs that determine wave packing. */
function dagFingerprint(dag: Dag): string {
	return JSON.stringify(
		(dag.nodes ?? []).map((n) => ({
			id: n.id,
			owns: n.owns ?? [],
			depends_on: n.depends_on ?? [],
		})),
	);
}

function firstNonDoneWave(state: CampaignState): number {
	const next = (state.waves ?? []).find((w) => w.status !== "done");
	return next ? next.index : (state.waves ?? []).length;
}

function currentWave(state: CampaignState): WaveState | undefined {
	const index = state.current_wave ?? firstNonDoneWave(state);
	return (state.waves ?? []).find((w) => w.index === index);
}

/** Nodes in the current wave whose dependencies are all done. */
function readyWaveNodes(dag: Dag, state: CampaignState): string[] {
	const wave = currentWave(state);
	if (!wave) return [];
	const ready = new Set(readyNodes(dag, state));
	return wave.members.filter((id) => ready.has(id));
}

/** Rebuild wave state from the engine's projection, preserving cleanup flags. */
function reconcileWaves(state: CampaignState, dagWaves: any[]): void {
	// Key prior cleanup flags by membership, not index, so adding a depends_on
	// edge (which can reindex waves) never skips or repeats a wave's cleanup.
	const prior = new Map<string, WaveState>(
		(state.waves ?? []).map((w) => [[...w.members].sort().join("|"), w]),
	);
	state.waves = (dagWaves ?? []).map((dw: any) => {
		const members: string[] = (dw.members ?? []).map((m: any) => String(m));
		const prev = prior.get([...members].sort().join("|"));
		const wave: WaveState = {
			index: Number(dw.wave),
			members,
			status: "pending",
			integrated: members.filter((id) => state.nodes[id]?.status === "done"),
			cleanup_done: prev?.cleanup_done ?? false,
		};
		wave.status = deriveWaveStatus(state, wave);
		return wave;
	});
	const byNode = new Map<string, number>();
	for (const wave of state.waves) {
		for (const id of wave.members) byNode.set(id, wave.index);
	}
	for (const id of Object.keys(state.nodes)) {
		if (byNode.has(id)) state.nodes[id].wave = byNode.get(id);
	}
	state.current_wave = firstNonDoneWave(state);
}

/**
 * A wave's status from its members' node statuses. `recorded` and `paused`
 * count as active, because those waves are still in progress.
 */
function deriveWaveStatus(state: CampaignState, wave: WaveState): WaveState["status"] {
	const members = wave.members ?? [];
	if (!members.length) return "pending";
	let integrated = 0;
	let active = false;
	for (const id of members) {
		const status = state.nodes[id]?.status;
		if (status === "done") integrated += 1;
		else if (status === "running" || status === "recorded" || status === "paused") {
			active = true;
		}
	}
	if (integrated === members.length) return "done";
	return active ? "running" : "pending";
}

/**
 * Recompute every wave from the live node statuses. Returns the waves that
 * changed from not-done to done in this pass. Call this after any node status
 * change, so `state.json` never reports a running wave as pending.
 */
function refreshWaves(state: CampaignState): WaveState[] {
	const completed: WaveState[] = [];
	for (const wave of state.waves ?? []) {
		wave.integrated = wave.members.filter((id) => state.nodes[id]?.status === "done");
		const status = deriveWaveStatus(state, wave);
		if (status === "done" && wave.status !== "done") completed.push(wave);
		wave.status = status;
	}
	state.current_wave = firstNonDoneWave(state);
	return completed;
}

function summarise(dag: Dag, state: CampaignState): string {
	const lines = [
		`campaign: ${dag.campaign ?? "(unnamed)"}`,
		`target:   ${state.target_branch ?? dag.feature_branch ?? "(unset)"}` +
			`  worktree: ${state.worktree_branch ?? "(unset)"}` +
			`  base: ${dag.base ?? state.base ?? "(unset)"}`,
		`design:   ${dag.design ?? "(unspecified)"}`,
		`nodes:    ${nodeIds(dag).length}  wave size: ${
			state.wave_size ?? dag.concurrency ?? "?"
		}`,
	];
	const waveOf = new Map<string, number>();
	for (const wave of state.waves ?? []) {
		for (const id of wave.members) waveOf.set(id, wave.index);
	}
	for (const id of nodeIds(dag)) {
		const node = (dag.nodes ?? []).find((n) => n.id === id);
		const wave =
			waveOf.get(id) ?? state.nodes[id]?.wave ?? "?";
		lines.push(
			`  w${wave} ${id} [${node?.phase ?? "-"}] ${nodeStatus(state, id)}` +
				(node?.label ? ` — ${node.label}` : ""),
		);
	}
	for (const wave of state.waves ?? []) {
		lines.push(`wave ${wave.index} [${wave.status}]: ${wave.members.join(", ")}`);
	}
	return lines.join("\n");
}

/** First non-empty line of a worker's final report, for the commit subject. */
function workerDescription(output: string): string {
	const line = String(output ?? "")
		.split("\n")
		.map((row) => row.replace(/^[\s#>*+-]+/, "").trim())
		.find((row) => row.length > 0);
	return (line ?? "").replace(/\s+/g, " ");
}

/** The descriptions the coordinator captured for a set of nodes. */
function nodeDescriptions(state: any, members: string[]): Record<string, string> {
	const messages: Record<string, string> = {};
	for (const id of members) {
		const description = state.nodes[id]?.description;
		if (description) messages[id] = description;
	}
	return messages;
}

export default function coordinatorExtension(pi: ExtensionAPI) {
	/**
	 * Make the campaign tools callable for this session. The tools register
	 * inactive (`defaultActive: false`), and pi does not restore the active set
	 * from a transcript when a session is resumed, so both the `/sliceme` command
	 * and the `session_start` resume hook must re-activate them. Idempotent: it
	 * merges into whatever is already active.
	 */
	function activateCampaignTools(): void {
		const active = new Set(pi.getActiveTools());
		active.add("sliceme");
		active.add("sliceme-unit");
		pi.setActiveTools([...active]);
	}

	// Extension-only entry point. The tools register inactive; `/sliceme
	// [DESIGN.md]` activates them and asks the model to start a campaign, and the
	// `session_start` hook re-activates them when a suspended campaign is resumed.
	// No design document is required up front: `start` fails loudly if the path is
	// wrong.
	pi.registerCommand("sliceme", {
		description: "Start a Sliceme campaign from a design document (default DESIGN.md)",
		handler: async (args, ctx) => {
			const design = args.trim() || "DESIGN.md";
			if (!ctx.isIdle()) {
				ctx.ui.notify("sliceme: the agent is busy; finish the current turn first.", "warning");
				return;
			}
			activateCampaignTools();
			ctx.ui.notify(`sliceme: starting a campaign from ${design}`, "info");
			pi.sendUserMessage(
				`Start a Sliceme campaign for the design document "${design}". ` +
					`Use the sliceme tool with action "start".`,
			);
		},
	});

	const sliceme = (ctx: ExtensionContext, args: string[], signal?: AbortSignal) =>
		runSliceme(pi, ctx, args, signal);

	async function featureBranch(ctx: ExtensionContext): Promise<string> {
		const { json } = await sliceme(ctx, ["status"]);
		const branch = json?.feature_branch ?? json?.main_branch;
		if (!branch) throw new Error("sliceme: no feature branch; run `sliceme start` first");
		return String(branch);
	}

	/** The branch currently checked out in the coordinator's checkout. */
	async function currentBranch(ctx: ExtensionContext): Promise<string> {
		const result = await pi.exec("git", ["symbolic-ref", "--quiet", "--short", "HEAD"], {
			cwd: ctx.cwd,
		});
		const branch = result.stdout?.trim();
		if (result.code !== 0 || !branch) {
			throw new Error(
				"sliceme: not on a branch (detached HEAD); check out the campaign branch first",
			);
		}
		return branch;
	}

	/**
	 * The repository default branch, mirroring `integrate.found_default_branch`:
	 * origin/HEAD, then init.defaultBranch, then an existing main/master.  The
	 * checked-out branch is deliberately not a fallback, because `start` adopts
	 * it as the feature branch.
	 */
	async function defaultBranch(ctx: ExtensionContext, feature: string): Promise<string> {
		const origin = await pi.exec(
			"git",
			["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
			{ cwd: ctx.cwd },
		);
		if (origin.code === 0 && origin.stdout?.trim()) {
			return origin.stdout.trim().replace(/^origin\//, "");
		}
		const configured = await pi.exec("git", ["config", "--get", "init.defaultBranch"], {
			cwd: ctx.cwd,
		});
		const name = configured.stdout?.trim();
		if (configured.code === 0 && name && name !== feature) return name;
		for (const candidate of ["main", "master"]) {
			if (candidate === feature) continue;
			const exists = await pi.exec(
				"git",
				["rev-parse", "--verify", "--quiet", `refs/heads/${candidate}`],
				{ cwd: ctx.cwd },
			);
			if (exists.code === 0) return candidate;
		}
		return "main";
	}

	function load(ctx: ExtensionContext, branch: string): { dag: Dag; state: CampaignState } {
		return {
			dag: readJson<Dag>(dagPath(ctx.cwd, branch), { nodes: [] }),
			state: stateStore(ctx, branch).read<CampaignState>(),
		};
	}

	// One store per campaign branch. Every state mutation in this process goes
	// through it, so parallel `spawn` completions cannot drop each other's writes
	// (`docs/observability.md` §9 suggestion 2).
	const stateStores = new Map<string, CampaignStateStore>();

	function stateStore(ctx: ExtensionContext, branch: string): CampaignStateStore {
		const file = statePath(ctx.cwd, branch);
		let store = stateStores.get(file);
		if (!store) {
			store = new CampaignStateStore(file);
			stateStores.set(file, store);
		}
		return store;
	}

	// ------------------------------------------------------------------
	// Live progress view (docs/observability.md §5, §7)
	// ------------------------------------------------------------------
	// One registry and one render timer per coordinator process. The engine owns
	// durable state; this registry owns the live rows. `runTracked` registers an
	// agent and folds `onProgress` into it, then the timer recomposes the widget
	// so elapsed time advances between events.
	const liveAgents = new Map<string, ProgressAgent>();
	let liveCampaign: Omit<ProgressSnapshot, "agents" | "now"> = { waves: [], nodes: [] };
	let liveCtx: ExtensionContext | undefined;
	let liveTimer: ReturnType<typeof setInterval> | undefined;
	const LIVE_RENDER_MS = 250;

	/** The only place that composes the widget; pure formatting lives in common.ts. */
	function renderLive(): void {
		const ctx = liveCtx;
		if (!ctx?.hasUI) return;
		const lines = renderProgress(
			{ ...liveCampaign, agents: [...liveAgents.values()], now: Date.now() / 1000 },
			{
				width: process.stdout.columns || undefined,
				color: (name, text) => ctx.ui.theme.fg(name as any, text),
			},
		);
		ctx.ui.setWidget("sliceme", lines);
	}

	/** Cache the campaign projection for the next render. */
	function liveCampaignFrom(dag: Dag, state: CampaignState): void {
		// Refresh derived wave statuses so a just-started worker never shows as pending.
		refreshWaves(state);
		liveCampaign = {
			campaign: dag.campaign ?? state.campaign,
			currentWave: state.current_wave,
			waves: (state.waves ?? []).map((wave) => ({
				index: wave.index,
				status: wave.status,
				members: wave.members,
			})),
			nodes: nodeIds(dag).map((id) => ({ id, status: nodeStatus(state, id) })),
		};
	}

	/** Start the timer while any agent runs; render once and stop otherwise. */
	function ensureLiveTimer(ctx: ExtensionContext): void {
		liveCtx = ctx;
		if (![...liveAgents.values()].some((agent) => agent.status === "running")) {
			stopLiveTimer();
			renderLive();
			return;
		}
		if (liveTimer) return;
		liveTimer = setInterval(renderLive, LIVE_RENDER_MS);
	}

	function stopLiveTimer(): void {
		if (!liveTimer) return;
		clearInterval(liveTimer);
		liveTimer = undefined;
	}

	// ------------------------------------------------------------------
	// Suspend / resume (docs/sessions.md)
	// ------------------------------------------------------------------
	function readControl(ctx: ExtensionContext, branch: string): any | null {
		try {
			const file = controlPath(ctx.cwd, branch);
			if (!fs.existsSync(file)) return null;
			return JSON.parse(fs.readFileSync(file, "utf8"));
		} catch {
			return null;
		}
	}

	/** True when a fresh pause flag is set for the campaign. */
	function isPaused(ctx: ExtensionContext, branch: string): boolean {
		const control = readControl(ctx, branch);
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

	function pausedResult(action: string): any {
		return {
			content: [
				{ type: "text" as const, text: `sliceme: paused before ${action}; resume when ready.` },
			],
			details: { paused: true, action },
		};
	}

	function buildSessionDescriptor(
		ctx: ExtensionContext,
		branch: string,
		over: { status?: string; reason?: string; label?: string } = {},
		stateOverride?: any,
	): any {
		const { dag, state: loaded } = load(ctx, branch);
		const state = stateOverride ?? loaded;
		let sessionId: string | undefined;
		let sessionFile: string | undefined;
		try {
			sessionId = ctx.sessionManager.getSessionId();
			sessionFile = ctx.sessionManager.getSessionFile();
		} catch {
			/* ephemeral (--no-session) runs have no session manager entry */
		}
		const nodes: Record<string, any> = {};
		for (const [id, raw] of Object.entries(state.nodes ?? {})) {
			const node = raw as NodeState;
			nodes[id] = {
				status: node.status,
				attempt: node.attempts ?? 0,
				candidate: node.candidate ?? null,
				commit: node.commit ?? null,
				last_heartbeat: heartbeatPath(ctx.cwd, branch, id),
			};
		}
		return {
			campaign: dag.campaign ?? state.campaign,
			feature_branch: branch,
			worktree_branch: state.worktree_branch,
			design: dag.design,
			pi: { session_id: sessionId, session_file: sessionFile, cwd: ctx.cwd },
			label: over.label ?? (state as any).label,
			status: over.status ?? "suspended",
			reason: over.reason ?? "user",
			suspended_at: Date.now() / 1000,
			current_wave: state.current_wave ?? null,
			waves: state.waves ?? [],
			nodes,
			resume_plan: (state as any).resume_plan ?? {},
		};
	}

	function writeSessionDescriptor(
		ctx: ExtensionContext,
		branch: string,
		over: { status?: string; reason?: string; label?: string } = {},
		stateOverride?: any,
	): any {
		const descriptor = buildSessionDescriptor(ctx, branch, over, stateOverride);
		writeJson(sessionPath(ctx.cwd, branch), descriptor);
		return descriptor;
	}

	/**
	 * Fetch the engine's resume plan (git plus ``state.db`` win over the
	 * descriptor).  Returns ``null`` when the engine cannot answer.
	 */
	async function fetchResumePlan(ctx: ExtensionContext, branch: string): Promise<any | null> {
		try {
			return (await sliceme(ctx, ["status", "--resume", "--plan-only"])).json;
		} catch {
			return null;
		}
	}

	/**
	 * Whether the plane still has campaign work for a resume to do.
	 *
	 * A ``suspended`` descriptor can outlive a finished campaign (crash, delivery
	 * from the CLI, or a done-but-undelivered plane).  A node is work when its
	 * resume status is not ``done``, or the plan asks for a record/verify/spawn.
	 * A null plan (engine unavailable) fails open so a real suspension is never
	 * hidden.
	 */
	function planHasWork(plan: any): boolean {
		if (!plan) return true;
		const statuses = Object.values(plan.nodes ?? {}) as string[];
		if (statuses.some((status) => status !== "done")) return true;
		const rp = plan.resume_plan ?? {};
		return (
			(rp.resume?.length ?? 0) > 0 ||
			(rp.respawn?.length ?? 0) > 0 ||
			(rp.verify?.length ?? 0) > 0 ||
			(rp.record_wave !== undefined && rp.record_wave !== null)
		);
	}

	// ------------------------------------------------------------------
	// Review relay (docs/review.md)
	// ------------------------------------------------------------------
	// A comment is delivered at least once: the ack is best-effort, so a crash
	// repeats a comment rather than losing it.  The in-process set only avoids a
	// duplicate inside one session.
	const relayedComments = new Set<number>();
	let reviewTimer: ReturnType<typeof setInterval> | undefined;
	let reviewCtx: ExtensionContext | undefined;
	let reviewServer: ReviewServerHandle | undefined;

	async function relayReviewComments(
		ctx: ExtensionContext,
		branch: string,
		signal?: AbortSignal,
	): Promise<void> {
		// Stop the review server when the campaign is delivered or no commits
		// remain.  This catches a delivery from the review client or the CLI, not
		// only the tool path.
		const teardownState = stateStore(ctx, branch).read<CampaignState>();
		if (reviewServer && !reviewNeeded(teardownState)) stopReviewServer(ctx, branch);
		if (!ctx.isIdle()) return;
		let payload: any;
		try {
			payload = (await sliceme(ctx, ["review", "--poll"], signal)).json;
		} catch {
			return;
		}
		const comments: any[] = payload?.comments ?? [];
		for (const comment of comments) {
			const id = Number(comment.id);
			if (!id || relayedComments.has(id)) continue;
			relayedComments.add(id);
			const where = [
				comment.commit_hash ? String(comment.commit_hash).slice(0, 7) : null,
				comment.file,
				comment.line,
			]
				.filter((value) => value !== null && value !== undefined)
				.join(" ");
			pi.sendUserMessage(
				`Review comment${where ? ` on ${where}` : ""}: ${String(comment.body ?? "")}`,
			);
			try {
				await sliceme(ctx, ["review", "--ack", "--comment-id", String(id)], signal);
			} catch {
				/* at-least-once: a failed ack repeats the comment, never loses it */
			}
		}
		// When every commit is approved and every wave is done, proceed
		// automatically; the human approval is the trigger, not a prompt.
		if (payload?.all_approved && ctx.isIdle()) {
			const state: any = stateStore(ctx, branch).read();
			if (allWavesDone(state) && !state.delivered) {
				await tryDelivery(ctx, branch, state, signal);
			}
		}
	}

	function startReviewTimer(): void {
		if (reviewTimer) return;
		reviewTimer = setInterval(() => {
			if (!reviewCtx) return;
			const branch = configuredBranch(reviewCtx.cwd) ?? "";
			if (!branch) return;
			void relayReviewComments(reviewCtx, branch);
		}, 15000);
	}

	/** Whether the campaign has a recorded commit for the review client. */
	function hasRecordedCommits(state: CampaignState): boolean {
		return Object.values(state.nodes ?? {}).some((node) => Boolean(node?.commit));
	}

	/**
	 * Whether the review client is still needed. Delivery merges every approved
	 * commit, so the review surface stops after a successful delivery.
	 */
	function reviewNeeded(state: CampaignState): boolean {
		return hasRecordedCommits(state) && !state.delivered;
	}

	/**
	 * Start the review server once when commits exist, and keep its URL in a
	 * widget. The server runs as a background child, so the coordinator never
	 * blocks. The engine opens the default browser when one is available.
	 */
	async function ensureReviewServer(ctx: ExtensionContext, branch: string): Promise<void> {
		if (reviewServer && reviewServer.child.exitCode === null) return;
		let handle: ReviewServerHandle;
		try {
			handle = await spawnReviewServer({
				cwd: ctx.cwd,
				urlFile: reviewUrlPath(ctx.cwd),
				logFile: reviewLogPath(ctx.cwd),
			});
		} catch {
			/* review is optional; the campaign must continue without it */
			return;
		}
		reviewServer = handle;
		if (ctx.hasUI) ctx.ui.setWidget("sliceme-review", [handle.url]);
		// Log only the loopback origin; the fragment holds the write token.
		logEvent(ctx.cwd, branch, "review.server", { url: handle.url.split("#")[0] });
	}

	/** Stop the background review server and clear its widget. */
	function stopReviewServer(ctx: ExtensionContext, branch?: string): void {
		const handle = reviewServer;
		reviewServer = undefined;
		if (ctx.hasUI) ctx.ui.setWidget("sliceme-review", undefined);
		if (!handle) return;
		const key = branch ?? configuredBranch(ctx.cwd);
		try {
			handle.child.kill("SIGTERM");
		} catch {
			/* the child may have exited already */
		}
		// Force-kill a child that ignores the terminate signal.  The parent-death
		// watchdog covers a crash; this covers a hung server.
		const force = setTimeout(() => {
			if (handle.child.exitCode === null) {
				try {
					handle.child.kill("SIGKILL");
				} catch {
					/* ignore */
				}
			}
		}, 3000);
		handle.child.once("exit", () => clearTimeout(force));
		if (key) logEvent(ctx.cwd, key, "review.stopped", {});
	}

	/**
	 * The continuation prompt.  It states the progress (open wave, wave count, and
	 * per-node status) from the engine's plan instead of telling the model to look
	 * it up with `status`.
	 */
	function resumePrompt(branch: string, descriptor: any, plan: any): string {
		const campaign = descriptor?.campaign ?? plan?.campaign ?? branch;
		const waves: any[] = descriptor?.waves ?? [];
		const statuses: Record<string, string> = plan?.nodes
			? plan.nodes
			: Object.fromEntries(
					Object.entries(descriptor?.nodes ?? {}).map(([id, node]: [string, any]) => [
						id,
						String(node?.status ?? "pending"),
					]),
				);
		const totalWaves = waves.length;
		const current =
			plan?.current_wave ??
			descriptor?.current_wave ??
			(waves.find((wave) => wave.status !== "done")?.index ?? totalWaves);
		const nodeIds = Object.keys(statuses);
		const done = Object.values(statuses).filter((status) => status === "done").length;

		const lines = [
			`Resume the suspended Sliceme campaign "${campaign}" on branch "${branch}".`,
			"",
			totalWaves
				? `Progress: wave ${Math.min(Number(current) + 1, totalWaves)} of ${totalWaves}; ` +
					`${done}/${nodeIds.length} nodes done.`
				: `Progress: ${done}/${nodeIds.length} nodes done.`,
		];
		if (totalWaves) {
			lines.push("Waves:");
			for (const wave of waves) {
				const members = (wave.members ?? []).map(String);
				const detail = members
					.map((id: string) => `${id} (${statuses[id] ?? "pending"})`)
					.join(", ");
				const marker = Number(wave.index) === Number(current) ? " <- next" : "";
				lines.push(`  wave ${wave.index} [${wave.status}]${marker}: ${detail}`);
			}
		}
		const rp = plan?.resume_plan ?? {};
		const remaining: string[] = [];
		if (rp.record_wave !== undefined && rp.record_wave !== null) {
			remaining.push(`record wave ${rp.record_wave} first`);
		}
		if (rp.resume?.length) remaining.push(`continue (edits preserved): ${rp.resume.join(", ")}`);
		if (rp.verify?.length) remaining.push(`re-verify: ${rp.verify.join(", ")}`);
		if (rp.respawn?.length) remaining.push(`respawn: ${rp.respawn.join(", ")}`);
		if (rp.blocked?.length) remaining.push(`blocked: ${rp.blocked.join(", ")}`);
		if (remaining.length) lines.push(`Resume plan: ${remaining.join("; ")}.`);
		lines.push(
			"Use the sliceme tool to continue (spawn ready nodes, record, verify). " +
				"Do not restart completed nodes.",
		);
		return lines.join("\n");
	}

	/** Run a subagent with attempt bookkeeping, a heartbeat, and a live row. */
	async function runTracked(
		ctx: ExtensionContext,
		branch: string,
		opts: {
			agent: string;
			node: string;
			unit?: string;
			attempt?: number;
			task: string;
			cwd: string;
			log?: string;
			signal?: AbortSignal;
			onUpdate?: AgentToolUpdateCallback;
		},
	): Promise<SubagentResult> {
		const attempt = opts.attempt ?? 1;
		const now = () => Date.now() / 1000;
		// Register the live row before the child starts, so the widget shows the
		// agent even while it connects to the provider.
		const live: ProgressAgent = {
			key: `${opts.agent}:${opts.node}`,
			node: opts.node,
			agent: opts.agent,
			unit: opts.unit,
			attempt,
			status: "running",
			progress: {
				node: opts.node,
				unit: opts.unit,
				attempt,
				agent: opts.agent,
				turns: 0,
				toolCalls: 0,
				tools: {},
				tokensIn: 0,
				tokensOut: 0,
				cost: 0,
				startedAt: now(),
				updatedAt: now(),
			},
		};
		liveAgents.set(live.key, live);
		ensureLiveTimer(ctx);
		// P0.6: a spawn streams its own row through the tool update channel.
		const streamRow = (progress: SubagentProgress) => {
			if (!opts.onUpdate) return;
			opts.onUpdate({
				content: [
					{
						type: "text" as const,
						text: renderAgentLine(live, {
							width: process.stdout.columns || undefined,
							now: now(),
						}),
					},
				],
				details: { node: opts.node, agent: opts.agent, progress },
			});
		};
		const begin = ["attempt", "--begin", "--node", opts.node, "--attempt", String(attempt)];
		if (opts.unit) begin.push("--unit", opts.unit);
		begin.push("--agent", opts.agent);
		try {
			await sliceme(ctx, begin, opts.signal);
		} catch {
			/* attempt bookkeeping must never block a worker */
		}
		const result = await runSubagent({
			agent: opts.agent,
			task: opts.task,
			cwd: opts.cwd,
			log: opts.log,
			signal: opts.signal,
			node: opts.node,
			unit: opts.unit,
			attempt,
			heartbeat: heartbeatPath(ctx.cwd, branch, opts.node),
			onProgress: (progress) => {
				live.progress = progress;
				renderLive();
				streamRow(progress);
			},
		});
		live.status = result.interrupted ? "interrupted" : result.exitCode === 0 ? "done" : "failed";
		live.finishedAt = now();
		ensureLiveTimer(ctx);
		try {
			await sliceme(
				ctx,
				[
					"attempt",
					"--end",
					"--node",
					opts.node,
					"--attempt",
					String(attempt),
					"--status",
					result.interrupted ? "interrupted" : result.exitCode === 0 ? "ok" : "failed",
					"--exit-code",
					String(result.exitCode),
				],
				opts.signal,
			);
		} catch {
			/* best-effort */
		}
		return result;
	}

	/** Extra prompt material for a node whose worker was interrupted mid-node. */
	async function continuationContext(
		ctx: ExtensionContext,
		branch: string,
		worktree: string,
		node: string,
		state: any,
	): Promise<string> {
		const resumeList: string[] = state?.resume_plan?.resume ?? [];
		const paused =
			state.nodes?.[node]?.status === "paused" || resumeList.includes(node);
		if (!paused) return "";
		const parts: string[] = [
			"\nThis node's previous worker was interrupted. Its edits are preserved in the " +
				"shared campaign worktree; continue from the current state, do not start over.",
		];
		try {
			const status = await pi.exec("git", ["status", "--porcelain"], { cwd: worktree });
			const diff = await pi.exec("git", ["diff", "--stat"], { cwd: worktree });
			const statusText = status.stdout?.trim();
			const diffText = diff.stdout?.trim();
			if (statusText) parts.push(`\nWorking-tree changes:\n${statusText.slice(0, 1200)}`);
			if (diffText) parts.push(`\nDiff stat:\n${diffText.slice(0, 800)}`);
		} catch {
			/* the worktree may be gone; the caller then falls back to a fresh spawn */
		}
		const heartbeat = readJson<any>(heartbeatPath(ctx.cwd, branch, node), undefined);
		if (heartbeat) {
			parts.push(
				`\nPrevious attempt: ${heartbeat.attempt ?? "?"}, turns ${heartbeat.turns ?? 0}, ` +
					`tools ${heartbeat.tool_calls ?? 0}, last tool "${heartbeat.last_tool ?? "?"}".`,
			);
			if (heartbeat.last_text) {
				parts.push(`\nLast assistant text: ${String(heartbeat.last_text).slice(0, 280)}`);
			}
		}
		return parts.join("");
	}

	/**
	 * Ensure `state.waves` matches the current DAG.  Waves are the engine's
	 * deterministic projection (`sliceme status` -> `dag_waves`); a coordinator-added
	 * `depends_on` edge changes the DAG fingerprint and triggers a replan.
	 * Replanning preserves each node's done/pending status and the per-wave
	 * cleanup flag, so a resume never re-runs finished work.
	 */
	async function ensureWaves(
		ctx: ExtensionContext,
		branch: string,
		dag: Dag,
		state: CampaignState,
	): Promise<void> {
		if (isPaused(ctx, branch)) return;
		const fingerprint = dagFingerprint(dag);
		if (state.waves?.length && state.dag_fingerprint === fingerprint) return;
		const { json } = await sliceme(ctx, ["status"]);
		if (json?.dag_waves_error) throw new Error(`sliceme: ${json.dag_waves_error}`);
		reconcileWaves(state, json?.dag_waves ?? []);
		state.dag_fingerprint = fingerprint;
		state.wave_size = Number(dag.concurrency ?? 3);
		stateStore(ctx, branch).save();
		logEvent(ctx.cwd, branch, "wave.replanned", {
			waves: (state.waves ?? []).map((w) => w.members),
		});
	}

	/**
	 * Refresh the campaign projection and compose one widget frame. The renderer
	 * in `common.ts` is pure; this function only fetches the state and draws it.
	 */
	function widget(ctx: ExtensionContext, dag: Dag, state: CampaignState): void {
		liveCtx = ctx;
		liveCampaignFrom(dag, state);
		renderLive();
	}

	// ------------------------------------------------------------------
	// Actions
	// ------------------------------------------------------------------

	/**
	 * Resolve and validate the project sandbox gate through the engine, recording
	 * the digest in campaign state + events.  Returns an error message on failure
	 * (the caller refuses to continue) or ``null`` on success.
	 */
	async function sandboxGate(
		ctx: ExtensionContext,
		branch: string,
		state: any,
		signal?: AbortSignal,
	): Promise<string | null> {
		let gate: any;
		try {
			gate = (await sliceme(ctx, ["exec", "--validate"], signal)).json;
		} catch (error) {
			return String((error as Error)?.message ?? error);
		}
		state.sandbox_digest = gate?.digest ?? null;
		state.sandbox_manifest = gate?.manifest ?? null;
		state.sandbox_required = Boolean(gate?.required);
		stateStore(ctx, branch).save();
		logEvent(ctx.cwd, branch, "sandbox.gate", {
			required: state.sandbox_required,
			digest: state.sandbox_digest,
			manifest: state.sandbox_manifest,
		});
		return null;
	}

	/**
	 * Ask the user which branch is the campaign target (feature) branch:
	 * the current branch, a named existing branch, or a new branch.  The default
	 * branch can never be chosen and there is no override.  In non-interactive
	 * modes, fall back to the `target` / `target_mode` tool parameters.
	 */
	async function chooseTargetBranch(
		ctx: ExtensionContext,
		params: any,
	): Promise<{ name: string; mode: "current" | "existing" | "new" }> {
		if (params.target) {
			const mode = (params.target_mode as "current" | "existing" | "new") ?? "existing";
			return { name: String(params.target), mode };
		}
		const current = await currentBranch(ctx);
		if (!ctx.hasUI) return { name: current, mode: "current" };
		const defaultBr = await defaultBranch(ctx, current);
		const choice = await ctx.ui.select(
			`Which branch should Sliceme use as the target (feature) branch?\n` +
				`Current: ${current}\n` +
				`The target can never be the default branch '${defaultBr}', main, or master.`,
			["current branch", "existing branch", "new branch"],
		);
		if (!choice) throw new Error("sliceme: target branch selection cancelled");
		if (choice === "current branch") return { name: current, mode: "current" };
		const isNew = choice === "new branch";
		const answer = await ctx.ui.input(
			isNew ? "New feature branch name" : "Existing feature branch name",
		);
		const name = (answer ?? "").trim();
		if (!name) throw new Error("sliceme: a target branch name is required");
		return { name, mode: isNew ? "new" : "existing" };
	}

	/** Ensure the single campaign worktree exists and return its details. */
	async function ensureCampaignWorktree(
		ctx: ExtensionContext,
		signal?: AbortSignal,
	): Promise<any> {
		const opened = await sliceme(ctx, ["wave", "--open"], signal);
		const unit = opened.json?.unit ?? {};
		if (!unit.worktree) throw new Error("sliceme: could not create the campaign worktree");
		return unit;
	}

	/**
	 * Read the recorded target/worktree branch from an existing plane, if any.
	 * Lets a resume reuse the branch chosen at the start of the campaign instead
	 * of asking again.
	 */
	async function existingPlane(
		ctx: ExtensionContext,
	): Promise<{ target?: string; worktree?: string } | null> {
		if (!fs.existsSync(path.join(stateDir(ctx.cwd), "config.json"))) return null;
		try {
			const { json } = await sliceme(ctx, ["status"]);
			return {
				target: json?.target_branch ?? json?.main_branch,
				worktree: json?.worktree_branch,
			};
		} catch {
			return null;
		}
	}

	async function startCampaign(
		ctx: ExtensionContext,
		params: any,
		signal?: AbortSignal,
	): Promise<any> {
		const design = String(params.design ?? "DESIGN.md");
		const campaign = String(params.campaign ?? path.basename(ctx.cwd));

		// The user chooses the target branch once; it is remembered for the whole
		// campaign.  Work accumulates on a separate campaign worktree branch and is
		// only merged to the target after all waves finish and the user approves.
		// A resume reuses the recorded target instead of asking again.
		const prior = await existingPlane(ctx);
		const resuming = Boolean(
			prior?.target &&
				!params.replan &&
				fs.existsSync(statePath(ctx.cwd, String(prior.target))),
		);
		const chosen =
			!params.target && resuming
				? { name: String(prior!.target), mode: "current" as const }
				: await chooseTargetBranch(ctx, params);
		const branch = chosen.name;
		const defaultBr = await defaultBranch(ctx, branch);
		if (branch === defaultBr || branch === "main" || branch === "master") {
			return {
				content: [
					{
						type: "text" as const,
						text:
							`sliceme: '${branch}' is a default branch. Sliceme never commits to ` +
							`main, master, or the repository default branch. Choose a feature ` +
							`branch instead.`,
					},
				],
				isError: true,
			};
		}
		const notice =
			`sliceme: target (feature) branch '${branch}' (${chosen.mode}); ` +
			`commits accumulate on a separate campaign worktree branch.`;
		if (ctx.hasUI) ctx.ui.notify(notice, "info");

		// 1. Plane with no coordinator unit; the engine records the chosen target
		// branch (and re-points an existing plane).  A new target is created here.
		await sliceme(
			ctx,
			["start", "--no-unit", "--target", branch, "--target-mode", chosen.mode],
			signal,
		);
		const plane = (await sliceme(ctx, ["status"], signal)).json;
		const worktreeBranch = String(plane?.worktree_branch ?? "");

		const dagFile = dagPath(ctx.cwd, branch);
		const store = stateStore(ctx, branch);

		// 2a. Resume: rebuild node status from git/state.db, which always win
		// over state.json. A node left `running` by a crash is reset.
		if (fs.existsSync(dagFile) && !params.replan) {
			const existing = readJson<Dag>(dagFile, { nodes: [] });
			if (existing?.nodes?.length) {
				// A resume rebuild reads the file again, in case another run changed it.
				const state: any = store.reload();
				const status = (await sliceme(ctx, ["status"], signal)).json;
				const candidates = status?.candidates ?? [];
				// Consult the engine's resume plan so a worker interrupted with
				// edits preserved in the shared worktree is continued, not restarted.
				let resumePlan: any = null;
				if (fs.existsSync(sessionPath(ctx.cwd, branch))) {
					try {
						// Read the plan before --open recreates a hand-deleted worktree,
						// so a lost worktree still maps to a fresh spawn, not a pause.
						resumePlan = (await sliceme(ctx, ["status", "--resume", "--plan-only"], signal)).json;
					} catch {
						resumePlan = null;
					}
				}
				await ensureCampaignWorktree(ctx, signal);
				clearPause(ctx, branch);
				for (const node of nodeIds(existing)) {
					const entry = state.nodes[node] ?? { status: "pending", attempts: 0 };
					const candidate = candidates
						.filter((c: any) => c.node === node)
						.pop();
					if (candidate) {
						entry.candidate = candidate.id;
						entry.commit = candidate.head_commit;
						entry.branch = candidate.branch;
					}
					const planned = resumePlan?.nodes?.[node];
					if (planned === "done" || candidate?.status === "landed") {
						entry.status = "done";
					} else if (planned === "paused") {
						// The worker was interrupted with edits preserved; continue it.
						entry.status = "paused";
					} else if (planned === "recorded") {
						entry.status = "recorded";
					} else if (
						entry.status === "running" ||
						entry.status === "recorded"
					) {
						entry.status = "pending";
						entry.attempts = (entry.attempts ?? 0) + 1;
					}
					state.nodes[node] = entry;
				}
				state.resume_plan = resumePlan?.resume_plan ?? null;
				state.campaign = existing.campaign ?? campaign;
				state.feature_branch = branch;
				state.target_branch = branch;
				state.worktree_branch = worktreeBranch;
				state.base = existing.base ?? params.base ?? branch;
				refreshWaves(state);
				store.save();
				await ensureWaves(ctx, branch, existing, state);
				const pendingWave = resumePlan?.resume_plan?.record_wave;
				if (
					pendingWave !== undefined &&
					pendingWave !== null &&
					resumePlan?.worktree_dirty
				) {
					try {
						const resumeWave = (state.waves ?? []).find(
							(w: any) => Number(w.index) === Number(pendingWave),
						);
						const recordArgs = ["wave", "--record", "--wave", String(pendingWave)];
						const messages = nodeDescriptions(state, resumeWave?.members ?? []);
						if (Object.keys(messages).length) {
							recordArgs.push("--messages", JSON.stringify(messages));
						}
						await sliceme(ctx, recordArgs, signal);
						logEvent(ctx.cwd, branch, "wave.record_on_resume", { wave: pendingWave });
					} catch {
						/* unowned or ambiguous edits: leave the record for a human/CLI */
					}
				}
				const resumeGateError = await sandboxGate(ctx, branch, state, signal);
				if (resumeGateError) {
					return {
						content: [
							{
								type: "text" as const,
								text: `sliceme: sandbox gate failed: ${resumeGateError}`,
							},
						],
						isError: true,
					};
				}
				logEvent(ctx.cwd, branch, "campaign.resumed", {
					running_reset: nodeIds(existing).filter(
						(id) => state.nodes[id]?.status === "pending",
					),
				});
				return {
					content: [{ type: "text" as const, text: `${notice}\n\n${summarise(existing, state)}` }],
					details: { dag: existing, state, resumed: true, feature_branch: branch },
				};
			}
		}

		// 2b. Planner writes dag.json (plane state, never committed).
		await ensureCampaignWorktree(ctx, signal);
		const task =
			`Read the design at ${design}. Produce a machine-readable execution DAG as the file ` +
			`${dagFile}. Use ONLY the Write tool for that file. The JSON shape is: ` +
			`{"campaign","feature_branch","base","design","concurrency","max_attempts","nodes":[` +
			`{"id","label","phase","goal","owns","depends_on","acceptance","gpu"}]}. ` +
			`Rules: the DAG is the only authored schedule; waves are derived from owns + ` +
			`depends_on with concurrency (default 3) as the per-wave cap. owns MUST be ` +
			`directories at the deepest subdirectory that contains each touched path ` +
			`(e.g. "dir:src/api", never files/symbols); a subtree overlap puts the later ` +
			`node in a later wave, so keep same-wave owns disjoint; ` +
			`"phase" is a display label only; ` +
			`route shared build files (BUILD, Cargo.toml, lockfiles) to an explicit aggregation ` +
			`node every touched component depends_on; merge nodes that own the same directory and ` +
			`sit on one dependency chain into a single node, because one node completes one ` +
			`cohesive directory change; do not split one directory across a chain of small nodes; ` +
			`each node lists its acceptance commands; ` +
			`gpu is "none","T1","T2" and only the verifier may use it. ` +
			`Project sandbox: look for sliceme.sandbox.json, .sliceme-sandbox.json, or ` +
			`tools/sliceme-sandbox.json. If one exists, add "sandbox":{"path":"<relative ` +
			`path>"} to the DAG. If the project clearly needs isolation (Dockerfile, ` +
			`devcontainer, CI) but has no manifest, set "sandbox_required": true; the ` +
			`campaign then fails until a human adds a manifest. Never invent a sandbox. ` +
			`Feature/target branch: ${branch}. Base: ${params.base ?? branch}. Design: ${design}.`;
		const planner = await runTracked(ctx, branch, {
			agent: "planner",
			node: "planner",
			unit: "planner",
			attempt: 1,
			task,
			cwd: ctx.cwd,
			log: path.join(stateDir(ctx.cwd), `${branchKey(branch)}.worker_planner.log`),
			signal,
		});
		if (planner.interrupted || isPaused(ctx, branch)) return pausedResult("planner");
		if (planner.exitCode !== 0 || !fs.existsSync(dagFile)) {
			return {
				content: [
					{ type: "text" as const, text: `planner failed:\n${planner.stderr || planner.output}` },
				],
				isError: true,
			};
		}
		// The engine normalizes dag.json before every wave projection. Project
		// once, then read the contracted DAG before seeding state.
		let projection: any = null;
		try {
			projection = (await sliceme(ctx, ["status"], signal)).json;
		} catch (error) {
			return {
				content: [
					{
						type: "text" as const,
						text: `sliceme: DAG projection failed: ${String((error as Error)?.message ?? error)}`,
					},
				],
				isError: true,
			};
		}
		const normalized = projection?.dag_merge;
		if (normalized?.merged && Object.keys(normalized.merged).length) {
			logEvent(ctx.cwd, branch, "dag.merged", normalized);
		}
		const dag = readJson<Dag>(dagFile, { nodes: [] });
		if (!dag?.nodes?.length) {
			return {
				content: [{ type: "text" as const, text: `planner did not write a DAG at ${dagFile}` }],
				isError: true,
			};
		}
		const state: any = {
			campaign,
			feature_branch: branch,
			target_branch: branch,
			worktree_branch: worktreeBranch,
			base: params.base ?? branch,
			nodes: {},
		};
		for (const node of dag.nodes) state.nodes[node.id] = { status: "pending", attempts: 0 };
		store.replace(state);
		store.save();
		await ensureWaves(ctx, branch, dag, state);
		const gateError = await sandboxGate(ctx, branch, state, signal);
		if (gateError) {
			return {
				content: [
					{
						type: "text" as const,
						text: `sliceme: sandbox gate failed: ${gateError}`,
					},
				],
				isError: true,
			};
		}
		logEvent(ctx.cwd, branch, params.replan ? "dag.replanned" : "dag.created", {
			nodes: nodeIds(dag),
		});
		return {
			content: [{ type: "text" as const, text: `${notice}\n\n${summarise(dag, state)}` }],
			details: { dag, state, feature_branch: branch },
		};
	}

	async function spawnNode(
		ctx: ExtensionContext,
		params: any,
		signal?: AbortSignal,
		onUpdate?: AgentToolUpdateCallback,
	): Promise<any> {
		const node = String(params.node ?? "");
		if (!node) throw new Error("spawn requires --node <id>");
		const { json } = await sliceme(ctx, ["status"]);
		const branch = String(json?.feature_branch ?? "main");
		const store = stateStore(ctx, branch);
		const dag = readJson<Dag>(dagPath(ctx.cwd, branch), { nodes: [] });
		const state: any = store.read();
		if (isPaused(ctx, branch)) return pausedResult("spawn");
		const spec = (dag.nodes ?? []).find((n) => n.id === node);
		if (!spec) throw new Error(`spawn: unknown node '${node}'`);
		await ensureWaves(ctx, branch, dag, state);

		const maxAttempts = Number(dag.max_attempts ?? 3);
		const attempts = Number(state.nodes[node]?.attempts ?? 0);
		if (attempts >= maxAttempts) {
			state.nodes[node] = { ...(state.nodes[node] ?? {}), status: "failed" };
			store.save();
			throw new Error(`spawn: node '${node}' exceeded max_attempts=${maxAttempts}`);
		}

		// A node may only start in the current wave, and only once every
		// dependency is integrated (done), not merely verified.
		const wave = currentWave(state);
		if (!wave || !wave.members.includes(node)) {
			throw new Error(
				`spawn: node '${node}' is scheduled in wave ${state.nodes[node]?.wave ?? "?"}; ` +
					`current wave is ${wave?.index ?? "(none)"}`,
			);
		}
		if (!new Set(readyWaveNodes(dag, state)).has(node)) {
			throw new Error(`spawn: node '${node}' is not ready in wave ${wave.index}`);
		}

		// Workers are pure editors in the one shared campaign worktree: they never
		// create a unit and never run git.  A re-spawn re-runs the worker against
		// the same worktree; the previous wave's files are already present.
		const attempt = attempts + 1;
		const unit = await ensureCampaignWorktree(ctx, signal);
		const worktree = String(unit.worktree);
		if (path.resolve(worktree) === path.resolve(ctx.cwd)) {
			throw new Error("spawn: campaign worktree must differ from the coordinator checkout");
		}

		state.nodes[node] = {
			...(state.nodes[node] ?? {}),
			status: "running",
			unit: String(unit.name ?? "campaign"),
			worktree,
			branch: String(unit.branch ?? state.worktree_branch ?? ""),
		};
		refreshWaves(state);
		store.save();
		logEvent(ctx.cwd, branch, "node.spawn", { node, unit: unit.name, attempt });
		widget(ctx, dag, state);

		const previousEvidence = state.nodes[node]?.lastError
			? `\nA previous attempt failed with this verifier evidence:\n${state.nodes[node].lastError}`
			: "";
		const continuation = await continuationContext(ctx, branch, worktree, node, state);
		const task =
			`You are a one-shot worker for DAG node "${node}" (${spec.label ?? ""}). ` +
			`Goal: ${spec.goal ?? ""}. You own these directories: ${(spec.owns ?? []).join(", ")}. ` +
			`Edit ONLY files inside your owned directories and then stop. Do NOT run git, do ` +
			`not commit, and do not run the test suite: the coordinator records the wave and ` +
			`the single executor runs the checks. Never use the GPU and never touch another ` +
			`node.` +
			previousEvidence +
			continuation;
		const result = await runTracked(ctx, branch, {
			agent: "worker",
			node,
			unit: String(unit.name ?? "campaign"),
			attempt,
			task,
			cwd: worktree,
			log: logPath(ctx.cwd, branch, node),
			signal,
			onUpdate,
		});
		if (result.interrupted || isPaused(ctx, branch)) {
			// A suspended worker keeps its edits in the shared campaign worktree; mark
			// the node paused so resume continues it instead of respawning from scratch.
			// Its attempt is not consumed: the next spawn reuses the same attempt number.
			state.nodes[node].status = "paused";
			refreshWaves(state);
			store.save();
			logEvent(ctx.cwd, branch, "node.suspended", {
				node,
				attempt,
				signal: result.signal ?? null,
			});
			widget(ctx, dag, state);
			return pausedResult("spawn");
		}
		state.nodes[node].status = result.exitCode === 0 ? "pending" : "failed";
		state.nodes[node].attempts = attempt;
		if (result.exitCode === 0) {
			const description = workerDescription(result.output);
			if (description) state.nodes[node].description = description;
		}
		refreshWaves(state);
		store.save();
		widget(ctx, dag, state);
		return {
			content: [{ type: "text" as const, text: `worker ${node} exited ${result.exitCode}` }],
			details: { node, unit: unit.name, exitCode: result.exitCode, output: result.output },
			isError: result.exitCode !== 0,
		};
	}

	/**
	 * Record the current wave: commit each node's changed paths on the campaign
	 * worktree, enforce ownership conformance, and register a candidates per node.
	 * No feature-branch mutation happens here.
	 */
	async function recordWave(
		ctx: ExtensionContext,
		params: any,
		signal?: AbortSignal,
	): Promise<any> {
		const branch = await featureBranch(ctx);
		if (isPaused(ctx, branch)) return pausedResult("record");
		const store = stateStore(ctx, branch);
		const dag = readJson<Dag>(dagPath(ctx.cwd, branch), { nodes: [] });
		const state: any = store.read();
		await ensureWaves(ctx, branch, dag, state);
		const wave = currentWave(state);
		if (!wave) throw new Error("record: no open wave to record");
		const recordArgs = ["wave", "--record", "--wave", String(wave.index)];
		const messages = nodeDescriptions(state, wave.members);
		if (Object.keys(messages).length) {
			recordArgs.push("--messages", JSON.stringify(messages));
		}
		const recorded = await sliceme(ctx, recordArgs, signal);
		const candidates: any[] = recorded.json?.candidates ?? [];
		const byNode = new Map<string, any>(
			candidates.map((c: any) => [String(c.node), c]),
		);
		for (const id of wave.members) {
			const candidate = byNode.get(id);
			if (!candidate) continue;
			state.nodes[id] = {
				...(state.nodes[id] ?? {}),
				status: "recorded",
				candidate: candidate.id,
				commit: candidate.head_commit,
				branch: candidate.branch,
				unit: String(recorded.json?.unit ?? "campaign"),
				worktree: recorded.json?.worktree,
			};
		}
		refreshWaves(state);
		store.save();
		widget(ctx, dag, state);
		logEvent(ctx.cwd, branch, "wave.recorded", {
			wave: wave.index,
			candidates: candidates.map((c: any) => ({ node: c.node, commit: c.head_commit })),
		});
		const summary = candidates.length
			? `wave ${wave.index} recorded: ${candidates.map((c: any) => c.node).join(", ")}`
			: `wave ${wave.index} recorded no changes`;
		// Between waves, deliver any review comments the human wrote.
		void relayReviewComments(ctx, branch);
		// The first recorded commit makes the review client available.
		if (reviewNeeded(state)) void ensureReviewServer(ctx, branch);
		return { content: [{ type: "text" as const, text: summary }], details: recorded.json ?? {} };
	}

	async function verifyNode(
		ctx: ExtensionContext,
		params: any,
		signal?: AbortSignal,
	): Promise<any> {
		const node = String(params.node ?? "");
		if (!node) throw new Error("verify requires --node <id>");
		const branch = await featureBranch(ctx);
		if (isPaused(ctx, branch)) return pausedResult("verify");
		const dag = readJson<Dag>(dagPath(ctx.cwd, branch), { nodes: [] });
		const store = stateStore(ctx, branch);
		const state: any = store.read();
		const spec = (dag.nodes ?? []).find((n) => n.id === node);
		if (!spec) throw new Error(`verify: unknown node '${node}'`);
		const commit = String(
			state.nodes[node]?.commit ?? state.nodes[node]?.branch ?? "",
		);
		if (!commit) {
			throw new Error(`verify: node '${node}' has no recorded commit; run record first`);
		}

		// The executor is the single runner: enqueue the node's acceptance at its
		// recorded commit, drain the queue, and wait for the result. The verifier
		// then judges that evidence instead of running anything itself.
		const submitArgs = [
			"exec",
			"--submit",
			"--source",
			`node:${node}`,
			"--commit",
			commit,
			"--gpu",
			spec.gpu ?? "none",
		];
		for (const cmd of spec.acceptance ?? []) submitArgs.push("--command", cmd);
		const submitted = await sliceme(ctx, submitArgs, signal);
		await sliceme(ctx, ["exec", "--run"], signal);
		const job = (
			await sliceme(
				ctx,
				["exec", "--wait", "--job", String(submitted.json?.job?.id), "--timeout", "3600"],
				signal,
			)
		).json?.job;

		const evidence =
			`Executor status: ${job?.status ?? "unknown"}\n` +
			`Executor fingerprint: ${job?.fingerprint ?? "-"}\n` +
			`${job?.output ?? submitted.text}`;
		const task =
			`Independently verify DAG node "${node}" from the executor's recorded evidence ` +
			`only. You are read-only: do not run commands and do not edit any file. Acceptance ` +
			`vector: ${(spec.acceptance ?? []).join(" ; ")}. GPU tier: ${spec.gpu ?? "none"}.\n\n` +
			`${evidence}\n\nReport a single line starting with "VERDICT: PASS" or ` +
			`"VERDICT: FAIL", then your reasoning grounded in the evidence.`;
		const result = await runTracked(ctx, branch, {
			agent: "verifier",
			node,
			unit: `verify:${node}`,
			attempt: Number(state.nodes[node]?.attempts ?? 1),
			task,
			cwd: ctx.cwd,
			log: path.join(stateDir(ctx.cwd), `${branchKey(branch)}.worker_verify_${node}.log`),
			signal,
		});
		if (result.interrupted || isPaused(ctx, branch)) {
			// Suspended mid-verification: leave the node `recorded` (state is untouched)
			// so resume re-verifies the same candidate instead of failing the node.
			return pausedResult("verify");
		}

		const passed =
			job?.status === "passed" &&
			result.exitCode === 0 &&
			/VERDICT:\s*PASS/i.test(result.output);
		state.nodes[node] = {
			...(state.nodes[node] ?? {}),
			status: passed ? "done" : "failed",
			verdict: passed ? "pass" : "fail",
			job: job?.id ?? null,
			...(passed ? {} : { lastError: result.output || job?.output }),
		};
		const completed = refreshWaves(state);
		store.save();
		widget(ctx, dag, state);
		logEvent(ctx.cwd, branch, "node.verdict", {
			node,
			passed,
			gpu: spec.gpu ?? "none",
			job: job?.id ?? null,
			completed_waves: completed.map((w) => w.index),
		});

		// Nothing is merged per wave.  When every wave is done, try the merge; the
		// engine refuses until the human approves every commit in the review client.
		let delivery: any = null;
		if (passed && allWavesDone(state)) {
			delivery = await tryDelivery(ctx, branch, state, signal);
		}
		return {
			content: [
				{
					type: "text" as const,
					text: `${node}: ${passed ? "PASS" : "FAIL"} (job ${job?.id ?? "?"})\n${result.output}`,
				},
			],
			details: { node, passed, job: job?.id ?? null, executor: job, delivery: delivery?.json ?? null },
			isError: !passed,
		};
	}

	function allWavesDone(state: CampaignState): boolean {
		const waves = state.waves ?? [];
		return waves.length > 0 && waves.every((w) => w.status === "done");
	}

	/**
	 * Merge the campaign worktree once every wave is done and every commit is
	 * approved.  The human approves commits in the review client; the engine
	 * refuses a merge until then.  A refusal is not an error: the review relay
	 * retries after the next approval.
	 */
	async function tryDelivery(
		ctx: ExtensionContext,
		branch: string,
		state: any,
		signal?: AbortSignal,
		cleanupOverride?: string,
	): Promise<any> {
		if (state.delivered || !allWavesDone(state)) return null;
		const store = stateStore(ctx, branch);
		const target = String(state.target_branch ?? branch);
		const source = String(state.worktree_branch ?? "");
		// Generate the report so the reviewer can read it before approving.
		try {
			await sliceme(ctx, ["review", "--report"], signal);
		} catch {
			/* the report is evidence for the reviewer, never a merge gate */
		}
		const cleanup = cleanupOverride !== undefined ? String(cleanupOverride) : "none";
		const args = ["deliver", "--target", target];
		if (cleanup !== "none") args.push("--cleanup", cleanup);
		let delivered: any;
		try {
			delivered = await sliceme(ctx, args, signal);
		} catch (error) {
			const message = String((error as Error)?.message ?? error);
			if (!message.includes("not-approved")) throw error;
			// Not approved yet: stay ready for the next review poll.
			state.ready_to_deliver = true;
			store.save();
			return null;
		}
		const failed = (delivered.json?.results ?? []).some((r: any) => r.status === "failed");
		state.ready_to_deliver = false;
		if (!failed) {
			state.delivered = true;
			// `cleanup: all` removes state.json; do not resurrect it.
			store.save(cleanup !== "all");
			// Terminal immediately: a crash before `session_shutdown` must not leave a
			// `suspended` descriptor that re-offers a finished campaign on startup.
			writeSessionDescriptor(
				ctx,
				branch,
				{ status: "completed", reason: "delivered" },
				state,
			);
			// Delivery merges every approved commit; the review surface is done.
			stopReviewServer(ctx, branch);
		} else {
			store.save();
		}
		logEvent(ctx.cwd, branch, failed ? "campaign.deliver_failed" : "campaign.delivered", {
			target,
			source,
			results: delivered.json?.results ?? [],
		});
		return delivered;
	}

	async function deliverCampaign(
		ctx: ExtensionContext,
		params: any,
		signal?: AbortSignal,
	): Promise<any> {
		const branch = await featureBranch(ctx);
		const state: any = stateStore(ctx, branch).read();
		const delivered = await tryDelivery(ctx, branch, state, signal, params.cleanup);
		if (!delivered) {
			return {
				content: [
					{
						type: "text" as const,
						text: state.delivered
							? "campaign already delivered"
							: "delivery not approved (or awaiting approval)",
					},
				],
				details: { delivered: Boolean(state.delivered) },
			};
		}
		return {
			content: [{ type: "text" as const, text: delivered.text }],
			details: delivered.json ?? {},
		};
	}

	pi.registerTool({
		name: "sliceme",
		label: "Sliceme",
		description:
			"Coordinate a design into landed work: start (choose target branch + planner), " +
			"status, ready, spawn (one-shot editor in the campaign worktree), record (commit the " +
			"wave), verify (executor runs; verifier judges), deliver (merge to the target after " +
			"approval), report, exec (sandbox gate, campaign worktree, check queue). The dag.json " +
			"plan is the only schedule; waves are a projection of it.",
		promptSnippet: "Drive an Sliceme campaign (start → spawn → record → verify → deliver)",
		promptGuidelines: [
			"The target (feature) branch is chosen once at start and is never main, master, or " +
				"the repository default branch. There is no override; refuse and re-choose instead.",
			"The DAG in dag.json is the only authored schedule; waves are its deterministic " +
				"projection (owns + depends_on, capped by concurrency). Merge nodes that share an " +
				"owned directory and sit on one dependency chain into a single node.",
			"Workers are pure editors in the one shared campaign worktree: they never run git.",
			"Spawn nodes only from the current wave; a later wave starts after the previous wave " +
				"is fully recorded and verified. Never recreate the worktree or rebase between waves.",
			"Spawn every ready node in the current wave together (issue the spawn calls in " +
				"one turn so they run in parallel); never exceed the wave cap.",
			"After all workers in the current wave finish editing, call `record` to commit the " +
				"wave onto the campaign worktree (per-node commits, ownership conformance).",
			"A node is ready only once every dependency is done, never merely verified.",
			"If a wave record is rejected for a path outside every node's owned dirs, widen that " +
				"node's owns (or add a depends_on edge) in dag.json; the next status/ready/spawn replans.",
			"Only the single executor runs checks (and only it may use the GPU); verifiers " +
				"judge the executor's recorded evidence. The sandbox gate must pass before verifying.",
			"Use `exec` for the sandbox gate (--validate) and the check queue " +
				"(--submit/--run/--wait); use `wave` for the campaign worktree (--open) and " +
				"recording a wave (--record).",
			"Do NOT merge to the target per wave. `deliver` runs automatically once every wave " +
				"is done and every commit is approved in the review client.",
		],
		// Inactive until `/sliceme` activates it, so a plain session never
		// advertises the campaign workflow or injects its guidelines.
		defaultActive: false,
		parameters: Type.Object({
			action: StringEnum(CAMPAIGN_ACTIONS),
			design: Type.Optional(Type.String({ description: "start: design document path" })),
			campaign: Type.Optional(Type.String({ description: "start: campaign name" })),
			base: Type.Optional(
				Type.String({ description: "start: base branch/ref (default: target branch)" }),
			),
			target: Type.Optional(
				Type.String({
					description: "start/deliver: target (feature) branch; never main or master",
				}),
			),
			target_mode: Type.Optional(
				StringEnum(["current", "existing", "new"] as const, {
					description: "start: how to resolve the target branch",
				}),
			),
			replan: Type.Optional(Type.Boolean({ description: "start: re-run the planner" })),
			node: Type.Optional(Type.String({ description: "node id for spawn/verify" })),
			narrative: Type.Optional(Type.String({ description: "report: what-changed/risks text" })),
			validate: Type.Optional(
				Type.Boolean({ description: "exec: validate the project sandbox gate" }),
			),
			gpu_required: Type.Optional(
				Type.Boolean({ description: "exec: with validate, require a GPU runner" }),
			),
			open: Type.Optional(
				Type.Boolean({ description: "wave: create or reuse the campaign worktree" }),
			),
			record: Type.Optional(
				Type.Boolean({ description: "wave: record a wave (conformance + per-node commits)" }),
			),
			run: Type.Optional(Type.Boolean({ description: "exec: drain the executor queue" })),
			submit: Type.Optional(Type.Boolean({ description: "exec: enqueue a check job" })),
			wait: Type.Optional(Type.Boolean({ description: "exec: wait for a job" })),
			cancel: Type.Optional(Type.Boolean({ description: "exec: cancel a queued job" })),
			job: Type.Optional(Type.String({ description: "exec: job id" })),
			source: Type.Optional(
				Type.String({ description: "exec: fingerprint source; deliver: worktree branch" }),
			),
			commit: Type.Optional(Type.String({ description: "exec: commit/ref to run at" })),
			command: Type.Optional(
				Type.Array(Type.String(), { description: "exec: check command (repeatable)" }),
			),
			sandbox: Type.Optional(
				StringEnum(["none", "bwrap", "unshare"] as const, {
					description: "exec: sandbox mode override",
				}),
			),
			gpu: Type.Optional(
				StringEnum(["none", "T1", "T2"] as const, { description: "exec: GPU tier" }),
			),
			ff: Type.Optional(
				Type.Boolean({ description: "deliver: allow a fast-forward instead of a merge commit" }),
			),
			cleanup: Type.Optional(
				StringEnum(["none", "worktrees", "all"] as const, {
					description: "deliver: post-merge cleanup (default: ask in interactive mode)",
				}),
			),
			priority: Type.Optional(Type.Number({ description: "exec: higher runs first" })),
			timeout: Type.Optional(Type.Number({ description: "exec: timeout seconds" })),
			wave: Type.Optional(Type.Number({ description: "exec: wave index" })),
			requester: Type.Optional(Type.String({ description: "exec: verifier id" })),
			limit: Type.Optional(Type.Number({ description: "exec: max jobs to drain" })),
			messages: Type.Optional(
				Type.String({ description: "wave record: JSON object of node id to description" }),
			),
			summary: Type.Optional(Type.String({ description: "wave record: candidate summary" })),
			poll: Type.Optional(
				Type.Boolean({ description: "review: print open comments and the newest decision" }),
			),
			ack: Type.Optional(Type.Boolean({ description: "review: acknowledge one comment" })),
			comment: Type.Optional(Type.Boolean({ description: "review: record a comment" })),
			decision: Type.Optional(
				StringEnum(["approve", "request_changes", "override"] as const, {
					description: "review: record a decision",
				}),
			),
			all: Type.Optional(
				Type.Boolean({ description: "review: with approve, approve every unapproved commit" }),
			),
			file: Type.Optional(Type.String({ description: "review: file path" })),
			side: Type.Optional(StringEnum(["old", "new"] as const, { description: "review: comment side" })),
			line: Type.Optional(Type.Number({ description: "review: line number" })),
			line_end: Type.Optional(Type.Number({ description: "review: end line for a range" })),
			body: Type.Optional(Type.String({ description: "review: comment body" })),
			note: Type.Optional(Type.String({ description: "review: decision note" })),
			actor: Type.Optional(Type.String({ description: "review: who recorded the decision" })),
			comment_id: Type.Optional(Type.Number({ description: "review: comment id for --ack" })),
		}),
		async execute(_toolCallId, params, signal, onUpdate, ctx) {
			switch (params.action as (typeof CAMPAIGN_ACTIONS)[number]) {
				case "start":
					return startCampaign(ctx, params, signal);
				case "status": {
					const branch = await featureBranch(ctx);
					const { dag, state } = load(ctx, branch);
					await ensureWaves(ctx, branch, dag, state);
					widget(ctx, dag, state);
					if (reviewNeeded(state)) void ensureReviewServer(ctx, branch);
					return {
						content: [{ type: "text" as const, text: summarise(dag, state) }],
						details: { dag, state },
					};
				}
				case "ready": {
					const branch = await featureBranch(ctx);
					if (isPaused(ctx, branch)) return pausedResult("ready");
					const { dag, state } = load(ctx, branch);
					await ensureWaves(ctx, branch, dag, state);
					const wave = currentWave(state);
					const ready = readyWaveNodes(dag, state);
					const label = wave ? `wave ${wave.index}` : "(no open wave)";
					return {
						content: [
							{
								type: "text" as const,
								text: ready.length ? `${label}: ${ready.join(", ")}` : `(${label}: none ready)`,
							},
						],
						details: { ready, wave: wave?.index, waves: state.waves },
					};
				}
				case "spawn":
					return spawnNode(ctx, params, signal, onUpdate);
				case "record":
					return recordWave(ctx, params, signal);
				case "verify":
					return verifyNode(ctx, params, signal);
				case "deliver":
					return deliverCampaign(ctx, params, signal);
				case "report": {
					const branch = await featureBranch(ctx);
					const { dag } = load(ctx, branch);
					const args = ["review", "--report"];
					if (params.narrative) args.push("--narrative", String(params.narrative));
					if (dag.design) args.push("--design", dag.design);
					const { json, text } = await sliceme(ctx, args, signal);
					return { content: [{ type: "text" as const, text }], details: json ?? {} };
				}
				case "exec":
				case "wave":
				case "review": {
					const keys =
						params.action === "exec"
							? EXEC_KEYS
							: params.action === "wave"
								? WAVE_KEYS
								: REVIEW_KEYS;
					const { json, text } = await sliceme(
						ctx,
						engineArgs(String(params.action), keys, params),
						signal,
					);
					return { content: [{ type: "text" as const, text }], details: json ?? {} };
				}
				default:
					throw new Error(`sliceme: unknown action '${params.action}'`);
			}
		},
	});

	// ------------------------------------------------------------------
	// Session suspend / resume (docs/sessions.md)
	// ------------------------------------------------------------------
	pi.registerCommand("suspend", {
		description: "Suspend the current Sliceme campaign and register it for resume",
		handler: async (args, ctx) => {
			const branch = configuredBranch(ctx.cwd);
			if (!branch || !fs.existsSync(dagPath(ctx.cwd, branch))) {
				ctx.ui.notify("sliceme: no campaign in this directory", "warning");
				return;
			}
			const label = args.trim() || undefined;
			writeJson(controlPath(ctx.cwd, branch), {
				pause: true,
				requested_at: Date.now() / 1000,
				label,
			});
			// Abort the in-flight turn instead of steering and waiting: a steering
			// message is only delivered at the next turn boundary, so it would not
			// arrive until the current node's worker had already finished.  ctx.abort()
			// aborts the tool signal, which kills the worker subagent and any engine
			// subprocess, so suspension lands at the next safe point within seconds.
			if (!ctx.isIdle()) {
				ctx.abort();
				await ctx.waitForIdle();
			}
			clearPause(ctx, branch);
			const descriptor = writeSessionDescriptor(ctx, branch, { reason: "user", label });
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
		reviewCtx = ctx;
		liveCtx = ctx;
		startReviewTimer();
		try {
			if (event.reason !== "resume" && event.reason !== "startup") return;
			const branch = configuredBranch(ctx.cwd);
			if (!branch) return;
			const descriptor = readJson<any>(sessionPath(ctx.cwd, branch), undefined);
			if (!descriptor || descriptor.status !== "suspended") return;
			// Self-heal a stale descriptor: a `suspended` status can outlive the
			// campaign (crash, CLI delivery, or a done-but-undelivered plane). Never
			// offer to resume work the plane already shows as finished.
			const plan = await fetchResumePlan(ctx, branch);
			if (!planHasWork(plan)) {
				const { state } = load(ctx, branch);
				clearPause(ctx, branch);
				writeJson(sessionPath(ctx.cwd, branch), {
					...descriptor,
					status: state.delivered ? "completed" : "ready",
					completed_at: Date.now() / 1000,
				});
				if (event.reason === "startup" && ctx.hasUI && !state.delivered) {
					ctx.ui.notify(
						"sliceme: campaign is complete but not delivered; run the sliceme tool " +
							"with action 'deliver'",
						"info",
					);
				}
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
				activateCampaignTools();
				pi.sendUserMessage(text);
				return;
			}
			if (!ctx.hasUI || !ctx.isIdle()) return;
			const ok = await ctx.ui.confirm("Resume Sliceme campaign?", text);
			if (ok) {
				activateCampaignTools();
				pi.sendUserMessage(text);
			}
		} catch {
			/* a resume hook must never break session startup */
		} finally {
			const branch = configuredBranch(ctx.cwd);
			if (branch) {
				const { state } = load(ctx, branch);
				if (reviewNeeded(state)) void ensureReviewServer(ctx, branch);
				void relayReviewComments(ctx, branch);
			}
		}
	});

	pi.on("session_shutdown", (event, ctx) => {
		// Fast and idempotent: stop the review child and the live timer, then write
		// the descriptor.
		if (reviewTimer) {
			clearInterval(reviewTimer);
			reviewTimer = undefined;
		}
		reviewCtx = undefined;
		stopLiveTimer();
		stopReviewServer(ctx);
		try {
			const branch = configuredBranch(ctx.cwd);
			if (!branch || !fs.existsSync(dagPath(ctx.cwd, branch))) return;
			const { state } = load(ctx, branch);
			const waves = state.waves ?? [];
			const allDone = waves.length > 0 && waves.every((w) => w.status === "done");
			writeSessionDescriptor(ctx, branch, {
				status: state.delivered ? "completed" : allDone ? "ready" : "suspended",
				reason: event.reason === "reload" ? "reload" : "user",
			});
		} catch {
			/* never block shutdown */
		}
	});
}

export { nodeIds, nodeStatus, readyNodes, summarise };
