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
 *   deliver          after every wave: ask approval, then open the delivery pull request
 *   report           `sliceme report` plus the coordinator's narrative
 *
 * All waves commit onto one campaign worktree branch; nothing lands on the
 * target branch until every wave is done and the user approves `deliver`.
 * `deliver` pushes the campaign branch and opens a pull request.
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
	addressingBatches,
	addressingSessionId,
	branchKey,
	CampaignStateStore,
	clearActiveCampaign,
	controlPath,
	dagPath,
	heartbeatPath,
	logEvent,
	logPath,
	parseCampaignPlan,
	readActiveCampaign,
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
	topCommands,
	writeActiveCampaign,
	writeJson,
} from "./common.ts";
import type {
	AddressingBatch,
	AddressingComment,
	AddressingResolve,
	ProgressAgent,
	ProgressSnapshot,
	ReviewServerHandle,
	SubagentProgress,
	SubagentResult,
} from "./common.ts";

export const CAMPAIGN_ACTIONS = [
	"start",
	"status",
	"plan",
	"ready",
	"spawn",
	"record",
	"verify",
	"deliver",
	"report",
	"exec",
	"wave",
	"review",
	"progress",
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
	"parent_comment_id",
	"addressing_commit",
	"reply",
	"addressed",
	"resolve",
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
	"commits",
	"command",
	"only",
	"sandbox",
	"gpu",
	"priority",
	"timeout",
	"wave",
	"requester",
	"limit",
] as const;

/** Parameter names the `wave` action forwards to the engine verb. */
const WAVE_KEYS = ["open", "record", "wave", "only", "messages", "summary"] as const;

/** Parameter names the `progress` action forwards to the engine verb. */
const PROGRESS_KEYS = ["node"] as const;

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

/**
 * The campaign this session owns.
 *
 * Order: the per-process pointer file, then the engine's config mirror.  The
 * pointer is authoritative when several campaigns share one plane; the mirror
 * is the fallback for a resumed session whose pointer was written by an
 * earlier process.
 */
function activeCampaign(cwd: string): string | undefined {
	return readActiveCampaign(cwd) ?? configuredBranch(cwd);
}

function readyNodes(dag: Dag, state: CampaignState): string[] {
	const done = new Set(nodeIds(dag).filter((id) => nodeStatus(state, id) === "done"));
	return nodeIds(dag).filter((id) => {
		if (nodeStatus(state, id) === "done" || nodeStatus(state, id) === "running") return false;
		const node = (dag.nodes ?? []).find((n) => n.id === id);
		return (node?.depends_on ?? []).every((dep) => done.has(dep));
	});
}

/**
 * Canonical repo-relative directory: `dir:src/api/` -> `src/api`, `` -> `.`.
 *
 * Mirrors `sliceme.ownership.normalize_dir` so the coordinator's single-writer
 * guard agrees with the engine's wave packing.
 */
function normalizeDir(spec: string): string {
	let text = String(spec ?? "").trim();
	const colon = text.indexOf(":");
	if (colon >= 0 && text.slice(0, colon).trim().toLowerCase() === "dir") {
		text = text.slice(colon + 1);
	}
	const segments: string[] = [];
	for (const part of text.replace(/\\/g, "/").split("/")) {
		if (!part || part === ".") continue;
		if (part === "..") segments.pop();
		else segments.push(part);
	}
	return segments.join("/") || ".";
}

/**
 * True when two owned directory sets overlap by subtree (equal, ancestor, or
 * descendant).  Mirrors `sliceme.ownership.owns_conflict`: two nodes whose
 * directories overlap must not run concurrently in the one shared campaign
 * worktree.
 */
function ownsOverlap(a: string[], b: string[]): boolean {
	const dirsA = new Set(a.map(normalizeDir));
	const dirsB = new Set(b.map(normalizeDir));
	for (const x of dirsA) {
		for (const y of dirsB) {
			if (x === y || x === "." || y === ".") return true;
			if (x.startsWith(`${y}/`) || y.startsWith(`${x}/`)) return true;
		}
	}
	return false;
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

/**
 * Per-node verdicts from one verifier report (one verify turn for a wave).
 *
 * The wave verifier is asked for one `NODE <id>: PASS|FAIL` line per node. A
 * report with no such line (the single-node classic `VERDICT:` line) applies
 * its PASS/FAIL to every requested node, so the old contract still works.
 */
function parseVerdicts(output: string, nodes: string[]): Record<string, boolean> {
	const verdicts: Record<string, boolean> = {};
	const line = /NODE\s+([A-Za-z0-9_.+-]+)\s*:\s*(PASS|FAIL)/gi;
	let match: RegExpExecArray | null;
	while ((match = line.exec(String(output ?? ""))) !== null) {
		verdicts[match[1]] = match[2].toUpperCase() === "PASS";
	}
	if (!Object.keys(verdicts).length) {
		const pass = /VERDICT:\s*PASS/i.test(String(output ?? ""));
		for (const node of nodes) verdicts[node] = pass;
	}
	return verdicts;
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
					`Use the sliceme tool with action "start", then drive the campaign ` +
					`without asking for permission: call "ready" and "spawn" (use "nodes") ` +
					`for every ready node, then record, verify the wave in one turn, and ` +
					`continue through the waves. Stop only at the human gates: the target-branch ` +
					`choice, an explicit user suspension, a failed sandbox gate, and the final ` +
					`commit review before delivery.`,
			);
		},
	});

	// Engine verbs that act on one campaign.  The active campaign is injected
	// as `--campaign` so a tool call never mixes two campaigns' state.  `start`
	// is excluded because it creates or resumes the campaign itself.
	const CAMPAIGN_SCOPED_ACTIONS = new Set([
		"status",
		"deliver",
		"review",
		"exec",
		"wave",
		"attempt",
		"progress",
	]);

	function withCampaign(ctx: ExtensionContext, args: string[]): string[] {
		const action = args[0];
		if (!CAMPAIGN_SCOPED_ACTIONS.has(action)) return args;
		if (args.includes("--campaign")) return args;
		const branch = activeCampaign(ctx.cwd);
		if (!branch) return args;
		return [...args, "--campaign", branch];
	}

	const sliceme = (ctx: ExtensionContext, args: string[], signal?: AbortSignal) =>
		runSliceme(pi, ctx, withCampaign(ctx, args), signal);

	/**
	 * The campaign's feature (target) branch.
	 *
	 * Read from the session pointer and the engine's config mirror first, so a
	 * spawn/record/verify does not start a `status` subprocess just to learn the
	 * branch. The engine is the fallback when neither local source is reachable.
	 */
	async function featureBranch(ctx: ExtensionContext): Promise<string> {
		const local = activeCampaign(ctx.cwd);
		if (local) return local;
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

	/** True when a local branch exists. */
	async function branchExists(ctx: ExtensionContext, branch: string): Promise<boolean> {
		const result = await pi.exec(
			"git",
			["rev-parse", "--verify", "--quiet", `refs/heads/${branch}`],
			{ cwd: ctx.cwd },
		);
		return result.code === 0;
	}

	/** Read a design document from the session cwd; empty when missing. */
	function readDesignFile(ctx: ExtensionContext, design: string): string {
		const file = path.isAbsolute(design) ? design : path.join(ctx.cwd, design);
		try {
			return fs.readFileSync(file, "utf8");
		} catch {
			return "";
		}
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
	// Review relay (docs/review.md, docs/review-plan.md §10)
	// ------------------------------------------------------------------
	// A comment is delivered at least once: the ack is best-effort, so a crash
	// repeats a comment rather than losing it.  The in-process set only avoids a
	// duplicate inside one session.
	const relayedComments = new Set<number>();
	// Campaign branches whose automatic delivery hit a hard refusal. Only a
	// re-opened campaign worktree clears the block.
	const deliveryBlocked = new Set<string>();
	let reviewTimer: ReturnType<typeof setInterval> | undefined;
	let reviewCtx: ExtensionContext | undefined;
	let reviewServer: ReviewServerHandle | undefined;

	/**
	 * Reopen the wave that owns *node* so an addressing attempt can spawn after
	 * the last wave. The node is reset to pending and its wave becomes the
	 * current wave; `spawnAddressing` restores the node to done after the record
	 * so `allWavesDone` stays true for the delivery gate.
	 */
	function reopenNodeWave(state: CampaignState, node: string): number {
		const wave = (state.waves ?? []).find((entry) => entry.members.includes(node));
		if (!wave) throw new Error(`addressing: node '${node}' is not in a wave`);
		state.current_wave = wave.index;
		wave.status = "pending";
		state.nodes[node] = { ...(state.nodes[node] ?? {}), status: "pending" };
		return wave.index;
	}

	/** The task prompt for one addressing turn (comment text plus node scope). */
	function addressingTask(
		branch: string,
		node: string | null,
		spec: CampaignNode | undefined,
		comments: AddressingComment[],
	): string {
		const lines: string[] = [];
		if (node) {
			lines.push(
				`You are the dedicated addressing subagent for campaign branch "${branch}". ` +
					`A review comment resolved to node "${node}" (${spec?.label ?? ""}). ` +
					`Goal: ${spec?.goal ?? ""}. You own these directories: ` +
					`${(spec?.owns ?? []).join(", ")}. Edit ONLY files inside your owned ` +
					`directories, then stop. Do NOT run git, do not commit, and do not run ` +
					`the test suite: the coordinator records one commit for this batch.`,
			);
		} else {
			lines.push(
				`You are the dedicated addressing subagent for campaign branch "${branch}". ` +
					`A review comment resolved to no node (a general or outside-owns ` +
					`comment). Do NOT edit any file and do NOT create a DAG node. Reply ` +
					`with the answer text only; the coordinator records one reply row per ` +
					`comment.`,
			);
		}
		lines.push("");
		for (const comment of comments) {
			const where = [comment.file, comment.line]
				.filter((value) => value !== null && value !== undefined)
				.join(":");
			lines.push(
				`Comment #${comment.id}${where ? ` (${where})` : ""}: ${String(comment.body ?? "")}`,
			);
		}
		lines.push(
			"",
			"This session is persistent: the earlier turns hold the thread, so use them " +
				"as context and answer the new comment(s) above.",
		);
		return lines.join("\n");
	}

	/** The reply text for a batch: the addressing subagent's final report. */
	function addressingReplyBody(result: SubagentResult, comments: AddressingComment[]): string {
		const text = String(result.output ?? "").trim();
		if (text) return text;
		return `Addressed ${comments.map((comment) => `#${comment.id}`).join(", ")}.`;
	}

	/**
	 * One addressing turn in the campaign's persistent addressing session.
	 *
	 * The session id is stable per campaign (`addressingSessionId`), so pi reopens
	 * the same session for every comment and the subagent keeps the thread context
	 * across comments and turns (`pi --session-id <id>`). The turn runs in the
	 * campaign worktree, so a code answer edits the same tree the wave record
	 * commits.
	 */
	async function runAddressingTurn(
		ctx: ExtensionContext,
		branch: string,
		worktree: string,
		node: string | null,
		spec: CampaignNode | undefined,
		comments: AddressingComment[],
		signal?: AbortSignal,
		attempt?: number,
	): Promise<SubagentResult> {
		const state: any = stateStore(ctx, branch).read();
		return await runTracked(ctx, branch, {
			agent: "addressing",
			node: node ?? "addressing",
			unit: node ? String(state.nodes?.[node]?.unit ?? "campaign") : "addressing",
			attempt: attempt ?? 1,
			task: addressingTask(branch, node, spec, comments),
			cwd: worktree,
			log: path.join(stateDir(ctx.cwd), `${branchKey(branch)}.addressing.log`),
			sessionId: addressingSessionId(branch),
			signal,
		});
	}

	/**
	 * Write one reply row and mark its root comment addressed (contract C).
	 * `commit` is set only when the reply answers with code.
	 */
	async function recordReply(
		ctx: ExtensionContext,
		branch: string,
		commentId: number,
		body: string,
		node: string | null,
		commit: string | null,
		signal?: AbortSignal,
	): Promise<void> {
		const replyArgs = ["review", "--reply", "--comment-id", String(commentId), "--body", body];
		if (node) replyArgs.push("--node", node);
		if (commit) replyArgs.push("--addressing-commit", commit);
		await sliceme(ctx, replyArgs, signal);
		logEvent(ctx.cwd, branch, "review.reply", {
			comment: commentId,
			node: node ?? null,
			commit: commit ?? null,
		});
		const addressedArgs = ["review", "--addressed", "--comment-id", String(commentId)];
		if (commit) addressedArgs.push("--addressing-commit", commit);
		await sliceme(ctx, addressedArgs, signal);
		logEvent(ctx.cwd, branch, "review.addressed", {
			comment: commentId,
			commit: commit ?? null,
		});
	}

	/** Record one reply row per comment in a batch (contract C). */
	async function replyBatch(
		ctx: ExtensionContext,
		branch: string,
		comments: AddressingComment[],
		result: SubagentResult,
		node: string | null,
		commit: string | null,
		signal?: AbortSignal,
	): Promise<void> {
		const body = addressingReplyBody(result, comments);
		for (const comment of comments) {
			await recordReply(ctx, branch, comment.id, body, node, commit, signal);
		}
	}

	/** Record one commit that answers a batch of comments on the same node. */
	async function recordAddressingCommit(
		ctx: ExtensionContext,
		branch: string,
		waveIndex: number,
		node: string,
		result: SubagentResult,
		comments: AddressingComment[],
		signal?: AbortSignal,
	): Promise<{
		commit: string | null;
		candidate: any;
		branch?: string;
		worktree?: string;
		candidates: any[];
	}> {
		const ids = comments.map((comment) => `#${comment.id}`).join(", #");
		const description = workerDescription(result.output) || `address review comment ${ids}`;
		const recorded = await sliceme(
			ctx,
			[
				"wave",
				"--record",
				"--wave",
				String(waveIndex),
				"--messages",
				JSON.stringify({ [node]: description }),
			],
			signal,
		);
		const candidates: any[] = recorded.json?.candidates ?? [];
		const candidate = candidates.find((entry) => String(entry.node) === node);
		return {
			commit: candidate ? String(candidate.head_commit) : null,
			candidate: candidate ?? null,
			branch: candidate?.branch,
			worktree: recorded.json?.worktree,
			candidates: candidates.map((entry) => ({ node: entry.node, commit: entry.head_commit })),
		};
	}

	/**
	 * Answer one addressing batch.
	 *
	 * A comment that resolves to a node reuses that node after the last wave: the
	 * wave is reopened, the node reset, and one addressing attempt is spawned
	 * outside the `max_attempts` cap (§10.0 item 2). No DAG node is added. The
	 * worker edits only its owned directories; the coordinator then records one
	 * commit for the batch and writes one reply row per comment. A comment with no
	 * resolved node only records reply rows; it never spawns a node.
	 */
	async function spawnAddressing(
		ctx: ExtensionContext,
		branch: string,
		batch: AddressingBatch,
		signal?: AbortSignal,
	): Promise<void> {
		const node = batch.node;
		const comments = batch.comments;
		if (!node) {
			// A general or outside-owns comment: reply rows only, no node spawn.
			const worktree = await ensureCampaignWorktree(ctx, signal);
			const result = await runAddressingTurn(ctx, branch, worktree, null, undefined, comments, signal);
			if (result.interrupted || result.exitCode !== 0) {
				throw new Error(`addressing: reply-only turn exited ${result.exitCode}`);
			}
			await replyBatch(ctx, branch, comments, result, null, null, signal);
			return;
		}

		const store = stateStore(ctx, branch);
		const state: any = store.read();
		const dag = readJson<Dag>(dagPath(ctx.cwd, branch), { nodes: [] });
		const spec = (dag.nodes ?? []).find((entry) => entry.id === node);
		if (!spec) throw new Error(`addressing: unknown node '${node}'`);
		await ensureWaves(ctx, branch, dag, state);

		const waveIndex = reopenNodeWave(state, node);
		const attempt = Number(state.nodes[node]?.attempts ?? 0) + 1;
		state.nodes[node] = { ...(state.nodes[node] ?? {}), status: "running", attempts: attempt };
		refreshWaves(state);
		store.save();
		logEvent(ctx.cwd, branch, "node.spawn", {
			node,
			attempt,
			addressing: true,
			wave: waveIndex,
			comments: comments.map((comment) => comment.id),
		});

		const restore = () => {
			state.nodes[node] = { ...(state.nodes[node] ?? {}), status: "done", attempts: attempt };
			refreshWaves(state);
			store.save();
		};

		const worktree = await ensureCampaignWorktree(ctx, signal);
		let result: SubagentResult;
		try {
			result = await runAddressingTurn(
				ctx,
				branch,
				worktree,
				node,
				spec,
				comments,
				signal,
				attempt,
			);
		} catch (error) {
			restore();
			throw error;
		}
		if (result.interrupted || result.exitCode !== 0) {
			restore();
			throw new Error(`addressing: node '${node}' attempt ${attempt} failed`);
		}

		const recorded = await recordAddressingCommit(
			ctx,
			branch,
			waveIndex,
			node,
			result,
			comments,
			signal,
		);
		const commit: string | null = recorded.commit;
		state.nodes[node] = {
			...(state.nodes[node] ?? {}),
			status: "done",
			attempts: attempt,
			...(recorded.candidate !== null ? { candidate: recorded.candidate } : {}),
			...(commit !== null
				? { commit, branch: recorded.branch, worktree: recorded.worktree }
				: {}),
		};
		refreshWaves(state);
		store.save();
		logEvent(ctx.cwd, branch, "wave.recorded", {
			wave: waveIndex,
			addressing: true,
			node,
			candidates: recorded.candidates,
		});

		await replyBatch(ctx, branch, comments, result, node, commit, signal);
	}

	async function relayReviewComments(
		ctx: ExtensionContext,
		branch: string,
		signal?: AbortSignal,
	): Promise<void> {
		const state = stateStore(ctx, branch).read<CampaignState>();
		// Stop the review server when the campaign is delivered or when the
		// server process has exited. This catches a delivery from the review
		// client or the CLI, not only the tool path.
		if (reviewServer && (!reviewNeeded(state) || reviewServer.child.exitCode !== null)) {
			stopReviewServer(ctx, branch);
		}
		if (!ctx.isIdle()) return;
		let payload: any;
		try {
			payload = (await sliceme(ctx, ["review", "--poll"], signal)).json;
		} catch {
			return;
		}
		// Open comments still need a first relay; `pending` comments were delivered
		// but not addressed, so a restart re-relays them (the re-relay is
		// idempotent). Both go to the dedicated addressing subagent.
		const open: AddressingComment[] = payload?.comments ?? [];
		const pending: AddressingComment[] = payload?.pending ?? [];
		const queue: Array<{ comment: AddressingComment; ack: boolean }> = [
			...open.map((comment) => ({ comment, ack: true })),
			...pending.map((comment) => ({ comment, ack: false })),
		];
		const fresh = queue.filter(({ comment }) => {
			const id = Number(comment.id);
			return id && !relayedComments.has(id);
		});

		let processed = false;
		if (fresh.length) {
			const resolves: AddressingResolve[] = [];
			for (const { comment } of fresh) {
				const id = Number(comment.id);
				// A comment that already names its node needs no `review --resolve`
				// round-trip: the poll payload is the same answer the engine returns.
				const explicit = (comment as any).node as string | undefined;
				let resolved: any = explicit
					? { comment: id, node: explicit, reason: "explicit" }
					: { comment: id, node: null, reason: "general" };
				if (!explicit) {
					try {
						resolved =
							(await sliceme(
								ctx,
								["review", "--resolve", "--comment-id", String(id)],
								signal,
							)).json ?? resolved;
					} catch {
						/* routing failed: the addressing subagent records a reply row only */
					}
				}
				resolves.push({
					comment: id,
					node: resolved?.node ?? null,
					reason: resolved?.reason ?? "general",
				});
				logEvent(ctx.cwd, branch, "review.comment", {
					comment: id,
					node: resolved?.node ?? null,
					reason: resolved?.reason ?? null,
					file: comment.file ?? null,
					relayed: true,
				});
			}
			// Deliver first, then address: `delivered` must not regress `addressed`.
			for (const { comment, ack } of fresh) {
				if (!ack) continue;
				try {
					await sliceme(
						ctx,
						["review", "--ack", "--comment-id", String(Number(comment.id))],
						signal,
					);
				} catch {
					/* at-least-once: a failed ack repeats the comment, never loses it */
				}
			}
			for (const { comment } of fresh) relayedComments.add(Number(comment.id));
			for (const batch of addressingBatches(
				fresh.map((entry) => entry.comment),
				resolves,
			)) {
				if (isPaused(ctx, branch)) break;
				try {
					await spawnAddressing(ctx, branch, batch, signal);
					processed = true;
				} catch (error) {
					// Leave the comment eligible for the next poll.
					for (const comment of batch.comments) relayedComments.delete(Number(comment.id));
					logEvent(ctx.cwd, branch, "review.addressing_failed", {
						node: batch.node,
						comments: batch.comments.map((comment) => comment.id),
						error: String((error as Error)?.message ?? error),
					});
				}
			}
		}

		// An addressing commit re-opens the campaign approval, so read the gate
		// again after a pass; otherwise the stale poll would deliver early.
		let gate = payload;
		if (processed) {
			try {
				gate = (await sliceme(ctx, ["review", "--poll"], signal)).json ?? payload;
			} catch {
				/* keep the stale payload; the next tick re-polls */
			}
		}
		const unaddressed: any[] = gate?.pending ?? [];
		// Nothing is left to review once the campaign is approved, no comment is
		// delivered-but-unaddressed, and no wave will add a commit.
		if (gate?.all_approved && !unaddressed.length && allWavesDone(state)) {
			stopReviewServer(ctx, branch);
		}
		// Deliver only when the campaign is approved and no comment is pending
		// (section D). The human approval is the trigger, not a prompt.
		if (gate?.all_approved && !unaddressed.length && ctx.isIdle()) {
			if (allWavesDone(state) && !state.delivered && !deliveryBlocked.has(branch)) {
				try {
					await tryDelivery(ctx, branch, state, signal);
				} catch (error) {
					const message = String((error as Error)?.message ?? error);
					deliveryBlocked.add(branch);
					logEvent(ctx.cwd, branch, "campaign.deliver_blocked", { error: message });
					if (ctx.hasUI) {
						ctx.ui.notify(`sliceme: automatic delivery stopped: ${message}`, "error");
					}
				}
			}
		}
	}

	/** Fire-and-forget relay; a relay failure must never crash the session. */
	function relaySafely(ctx: ExtensionContext, branch: string): void {
		void relayReviewComments(ctx, branch).catch((error) => {
			logEvent(ctx.cwd, branch, "review.relay_failed", {
				error: String((error as Error)?.message ?? error),
			});
		});
	}

	function startReviewTimer(): void {
		if (reviewTimer) return;
		reviewTimer = setInterval(() => {
			const ctx = reviewCtx;
			if (!ctx) return;
			const branch = activeCampaign(ctx.cwd) ?? "";
			if (!branch) return;
			relaySafely(ctx, branch);
		}, 15000);
	}

	/** Whether the campaign has a recorded commit for the review client. */
	function hasRecordedCommits(state: CampaignState): boolean {
		return Object.values(state.nodes ?? {}).some((node) => Boolean(node?.commit));
	}

	/**
	 * Whether the review client is still needed. Delivery opens a pull request for
	 * every approved commit, so the review surface stops after a successful delivery.
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
		// Do not restart the review surface once the campaign is approved, no
		// comment is delivered-but-unaddressed, and no wave remains. A delivery
		// from the review client does not update state.json, so the engine poll is
		// the source of truth here.
		if (allWavesDone(stateStore(ctx, branch).read<CampaignState>())) {
			try {
				const payload = (await sliceme(ctx, ["review", "--poll"])).json;
				if (payload?.all_approved && !(payload?.pending ?? []).length) return;
			} catch {
				/* fall through and start the server */
			}
		}
		let handle: ReviewServerHandle;
		try {
			handle = await spawnReviewServer({
				cwd: ctx.cwd,
				urlFile: reviewUrlPath(ctx.cwd),
				logFile: reviewLogPath(ctx.cwd),
				campaign: branch,
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
		const key = branch ?? activeCampaign(ctx.cwd);
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
			/** Reopen this pi session instead of `--no-session` (addressing subagent). */
			sessionId?: string;
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
				toolSeconds: 0,
				toolDurations: {},
				commands: {},
				tokensIn: 0,
				tokensOut: 0,
				cost: 0,
				startedAt: now(),
				updatedAt: now(),
			},
		};
		liveAgents.set(live.key, live);
		ensureLiveTimer(ctx);
		let lastProgress = live.progress;
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
			sessionId: opts.sessionId,
			heartbeat: heartbeatPath(ctx.cwd, branch, opts.node),
			onProgress: (progress) => {
				lastProgress = progress;
				live.progress = progress;
				renderLive();
				streamRow(progress);
			},
		});
		lastProgress = live.progress;
		live.status = result.interrupted ? "interrupted" : result.exitCode === 0 ? "done" : "failed";
		live.finishedAt = now();
		ensureLiveTimer(ctx);
		const endArgs = [
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
			"--turns",
			String(lastProgress.turns),
			"--tool-calls",
			String(lastProgress.toolCalls),
			"--tools",
			JSON.stringify(lastProgress.tools ?? {}),
			"--tool-seconds",
			String(lastProgress.toolSeconds ?? 0),
			"--tool-durations",
			JSON.stringify(lastProgress.toolDurations ?? {}),
			"--slowest-commands",
			JSON.stringify(topCommands(lastProgress.commands ?? {})),
			"--tokens-in",
			String(lastProgress.tokensIn ?? 0),
			"--tokens-out",
			String(lastProgress.tokensOut ?? 0),
			"--cost",
			String(lastProgress.cost ?? 0),
		];
		if (lastProgress.lastTool) endArgs.push("--last-tool", lastProgress.lastTool);
		try {
			await sliceme(ctx, endArgs, opts.signal);
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

	/**
	 * The opened campaign worktree per branch, so a wave opens it once and later
	 * spawns reuse it instead of starting another `wave --open` subprocess.
	 */
	const campaignWorktrees = new Map<string, any>();

	/** Ensure the single campaign worktree exists and return its details. */
	async function ensureCampaignWorktree(
		ctx: ExtensionContext,
		signal?: AbortSignal,
	): Promise<any> {
		const branch = activeCampaign(ctx.cwd);
		const cached = branch ? campaignWorktrees.get(branch) : undefined;
		if (cached?.worktree && fs.existsSync(String(cached.worktree))) {
			if (branch) deliveryBlocked.delete(branch);
			return cached;
		}
		const opened = await sliceme(ctx, ["wave", "--open"], signal);
		const unit = opened.json?.unit ?? {};
		if (!unit.worktree) throw new Error("sliceme: could not create the campaign worktree");
		// A re-opened worktree restores the source branch, so allow delivery again.
		if (branch) {
			campaignWorktrees.set(branch, unit);
			deliveryBlocked.delete(branch);
		}
		return unit;
	}

	/**
	 * Read the session's campaign from the engine, if any.  The pointer file
	 * names the campaign; the engine's config mirror is the fallback.
	 */
	async function existingCampaign(
		ctx: ExtensionContext,
	): Promise<{ target?: string; worktree?: string } | null> {
		if (!fs.existsSync(path.join(stateDir(ctx.cwd), "config.json"))) return null;
		try {
			const branch = activeCampaign(ctx.cwd);
			const { json } = await sliceme(
				ctx,
				branch ? ["status", "--campaign", branch] : ["status"],
			);
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

		// A design may declare a campaign split.  Each entry becomes one campaign
		// with its own target branch and directory scope.  The operator runs the
		// entries in order; a directory may repeat across entries because the
		// campaign boundary resets ownership.
		const plan = parseCampaignPlan(readDesignFile(ctx, design));
		let planEntry = plan.find((entry) => entry.name === String(params.campaign ?? ""));
		if (!planEntry && plan.length) {
			planEntry = plan.find((entry) => !fs.existsSync(statePath(ctx.cwd, entry.target)));
		}
		if (plan.length && !planEntry) {
			return {
				content: [
					{
						type: "text" as const,
						text: `sliceme: every campaign in ${design} is already started.`,
					},
				],
				isError: true,
			};
		}

		// The user chooses the target branch once; it is remembered for the whole
		// campaign.  Work accumulates on a separate campaign worktree branch and is
		// only opened as a pull request after all waves finish and the user approves.
		// A resume reuses the recorded target instead of asking again.  A plan entry
		// supplies the target and the base, so there is nothing to choose.
		const prior = await existingCampaign(ctx);
		const resuming = Boolean(
			prior?.target &&
				!params.replan &&
				fs.existsSync(statePath(ctx.cwd, String(prior.target))),
		);
		const chosen = planEntry
			? {
					name: planEntry.target,
					mode: ((await branchExists(ctx, planEntry.target))
						? "existing"
						: "new") as "existing" | "new",
				}
			: !params.target && resuming
				? { name: String(prior!.target), mode: "current" as const }
				: await chooseTargetBranch(ctx, params);
		const branch = chosen.name;
		const planDirs = planEntry?.dirs ?? [];
		const planNotice = planEntry
			? `sliceme: campaign '${planEntry.name}' of ${plan.length} in the plan` +
				(planDirs.length ? `; scope: ${planDirs.join(", ")}` : "") +
				". "
			: "";
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
			`${planNotice}target (feature) branch '${branch}' (${chosen.mode}); ` +
			`commits accumulate on a separate campaign worktree branch.`;
		if (ctx.hasUI) ctx.ui.notify(notice, "info");

		// 1. Plane with no coordinator unit; the engine records the chosen target
		// branch (and re-points an existing plane).  A new target is created here.
		const startArgs = [
			"start",
			"--no-unit",
			"--target",
			branch,
			"--target-mode",
			chosen.mode,
		];
		if (planEntry?.base) startArgs.push("--base", planEntry.base);
		await sliceme(ctx, startArgs, signal);
		// Bind this session to the campaign so every later engine call names it.
		writeActiveCampaign(ctx.cwd, branch);
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
			(planDirs.length
				? `Campaign scope: this campaign owns only these directories: ${planDirs.join(", ")}. ` +
					`Plan every node inside that scope; a later campaign owns the rest. `
				: "") +
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

	/** One worker launch's shared wave context (resolved once per spawn call). */
	interface SpawnContext {
		branch: string;
		dag: Dag;
		state: any;
		store: CampaignStateStore;
		unit: any;
		worktree: string;
	}

	/**
	 * Spawn one or more ready nodes in one call.
	 *
	 * `--nodes` (or every ready node of the current wave when neither `--node`
	 * nor `--nodes` is given) fans the workers out concurrently: the worktree is
	 * opened once, the branch resolved once, and each worker runs as its own
	 * subagent. Readiness is the gate (DEC-2); waves stay a display hint, so an
	 * explicitly named later node may start once its dependencies are done.
	 */
	async function spawnNodes(
		ctx: ExtensionContext,
		params: any,
		signal?: AbortSignal,
		onUpdate?: AgentToolUpdateCallback,
	): Promise<any> {
		const branch = await featureBranch(ctx);
		const store = stateStore(ctx, branch);
		const dag = readJson<Dag>(dagPath(ctx.cwd, branch), { nodes: [] });
		const state: any = store.read();
		if (isPaused(ctx, branch)) return pausedResult("spawn");
		await ensureWaves(ctx, branch, dag, state);

		const picked: string[] = Array.isArray(params.nodes) && params.nodes.length
			? params.nodes.map((id: any) => String(id))
			: params.node
				? [String(params.node)]
				: readyWaveNodes(dag, state);
		const requested: string[] = [...new Set(picked)];
		if (!requested.length) throw new Error("spawn: no ready node in the current wave");

		const ready = new Set(readyNodes(dag, state));
		const maxAttempts = Number(dag.max_attempts ?? 3);
		const targets: CampaignNode[] = [];
		for (const id of requested) {
			const spec = (dag.nodes ?? []).find((n) => n.id === id);
			if (!spec) throw new Error(`spawn: unknown node '${id}'`);
			const attempts = Number(state.nodes[id]?.attempts ?? 0);
			if (attempts >= maxAttempts) {
				state.nodes[id] = { ...(state.nodes[id] ?? {}), status: "failed" };
				store.save();
				throw new Error(`spawn: node '${id}' exceeded max_attempts=${maxAttempts}`);
			}
			if (!ready.has(id)) {
				const waiting = (spec.depends_on ?? []).filter(
					(dep) => nodeStatus(state, dep) !== "done",
				);
				throw new Error(
					`spawn: node '${id}' is not ready` +
						(waiting.length ? ` (waiting on ${waiting.join(", ")})` : ""),
				);
			}
			targets.push(spec);
		}

		// One writer at a time: readiness is the spawn gate (DEC-2) and a named
		// later node may start early, so enforce directory disjointness here rather
		// than trusting wave membership.  Two concurrent workers that own the same
		// directory (or one that a still-running node owns) would edit the one
		// shared worktree and overwrite each other.
		const running = (dag.nodes ?? []).filter(
			(n) => nodeStatus(state, n.id) === "running",
		);
		for (let i = 0; i < targets.length; i += 1) {
			for (let j = i + 1; j < targets.length; j += 1) {
				if (ownsOverlap(targets[i].owns ?? [], targets[j].owns ?? [])) {
					throw new Error(
						`spawn: nodes '${targets[i].id}' and '${targets[j].id}' own overlapping ` +
							`directories; keep one writer per directory`,
					);
				}
			}
			const clash = running.find((n) =>
				ownsOverlap(targets[i].owns ?? [], n.owns ?? []),
			);
			if (clash) {
				throw new Error(
					`spawn: node '${targets[i].id}' owns a directory that running node ` +
						`'${clash.id}' owns; wait for it to finish`,
				);
			}
		}

		// Workers are pure editors in the one shared campaign worktree: they never
		// create a unit and never run git.  Open the worktree once for the wave.
		const unit = await ensureCampaignWorktree(ctx, signal);
		const worktree = String(unit.worktree);
		if (path.resolve(worktree) === path.resolve(ctx.cwd)) {
			throw new Error("spawn: campaign worktree must differ from the coordinator checkout");
		}

		for (const spec of targets) {
			const attempt = Number(state.nodes[spec.id]?.attempts ?? 0) + 1;
			state.nodes[spec.id] = {
				...(state.nodes[spec.id] ?? {}),
				status: "running",
				unit: String(unit.name ?? "campaign"),
				worktree,
				branch: String(unit.branch ?? state.worktree_branch ?? ""),
			};
			logEvent(ctx.cwd, branch, "node.spawn", { node: spec.id, unit: unit.name, attempt });
		}
		refreshWaves(state);
		store.save();
		widget(ctx, dag, state);

		const context: SpawnContext = { branch, dag, state, store, unit, worktree };
		const results = await Promise.all(
			targets.map((spec) => runWorker(ctx, context, spec, signal, onUpdate)),
		);
		if (results.some((result) => result.paused)) return pausedResult("spawn");
		const failed = results.filter((result) => result.exitCode !== 0);
		const summary = results
			.map((result) => `worker ${result.node} exited ${result.exitCode}`)
			.join("\n");
		return {
			content: [{ type: "text" as const, text: summary }],
			details: {
				nodes: results.map((result) => result.node),
				unit: unit.name,
				results: results.map((result) => ({
					node: result.node,
					exitCode: result.exitCode,
					output: result.output,
				})),
			},
			isError: failed.length > 0,
		};
	}

	/** Run one worker against the shared wave context and fold its result in. */
	async function runWorker(
		ctx: ExtensionContext,
		sc: SpawnContext,
		spec: CampaignNode,
		signal?: AbortSignal,
		onUpdate?: AgentToolUpdateCallback,
	): Promise<{ node: string; exitCode: number; output: string; paused: boolean }> {
		const node = spec.id;
		const attempt = Number(sc.state.nodes[node]?.attempts ?? 0) + 1;
		const previousEvidence = sc.state.nodes[node]?.lastError
			? `\nA previous attempt failed with this verifier evidence:\n${sc.state.nodes[node].lastError}`
			: "";
		const continuation = await continuationContext(
			ctx,
			sc.branch,
			sc.worktree,
			node,
			sc.state,
		);
		const task =
			`You are a one-shot worker for DAG node "${node}" (${spec.label ?? ""}). ` +
			`Goal: ${spec.goal ?? ""}. You own these directories: ${(spec.owns ?? []).join(", ")}. ` +
			`Edit ONLY files inside your owned directories and then stop. Do NOT run git, do ` +
			`not commit, and do not run the test suite: the coordinator records the wave and ` +
			`the single executor runs the checks. Never use the GPU and never touch another ` +
			`node.` +
			previousEvidence +
			continuation;
		const result = await runTracked(ctx, sc.branch, {
			agent: "worker",
			node,
			unit: String(sc.unit.name ?? "campaign"),
			attempt,
			task,
			cwd: sc.worktree,
			log: logPath(ctx.cwd, sc.branch, node),
			signal,
			onUpdate,
		});
		if (result.interrupted || isPaused(ctx, sc.branch)) {
			// A suspended worker keeps its edits in the shared campaign worktree; mark
			// the node paused so resume continues it instead of respawning from scratch.
			// Its attempt is not consumed: the next spawn reuses the same attempt number.
			sc.state.nodes[node].status = "paused";
			refreshWaves(sc.state);
			sc.store.save();
			logEvent(ctx.cwd, sc.branch, "node.suspended", {
				node,
				attempt,
				signal: result.signal ?? null,
			});
			widget(ctx, sc.dag, sc.state);
			return { node, exitCode: result.exitCode, output: result.output, paused: true };
		}
		sc.state.nodes[node].status = result.exitCode === 0 ? "pending" : "failed";
		sc.state.nodes[node].attempts = attempt;
		if (result.exitCode === 0) {
			const description = workerDescription(result.output);
			if (description) sc.state.nodes[node].description = description;
		}
		refreshWaves(sc.state);
		sc.store.save();
		widget(ctx, sc.dag, sc.state);
		return { node, exitCode: result.exitCode, output: result.output, paused: false };
	}

	/**
	 * Record the current wave, one member at a time.
	 *
	 * Each member records with E2's per-node filter (`--only`), so a stray or
	 * ambiguous path fails only the node it is attributed to and a path owned by
	 * another node is ignored. No feature-branch mutation happens here.
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
		const messages = nodeDescriptions(state, wave.members);
		const candidates: any[] = [];
		const failures: string[] = [];
		for (const id of wave.members) {
			const recordArgs = ["wave", "--record", "--wave", String(wave.index), "--only", id];
			if (messages[id]) recordArgs.push("--messages", JSON.stringify({ [id]: messages[id] }));
			let recorded: any;
			try {
				recorded = await sliceme(ctx, recordArgs, signal);
			} catch (error) {
				const message = String((error as Error)?.message ?? error);
				failures.push(`${id}: ${message}`);
				state.nodes[id] = { ...(state.nodes[id] ?? {}), status: "failed", lastError: message };
				continue;
			}
			const created: any[] = recorded.json?.candidates ?? [];
			candidates.push(...created);
			const candidate = created.find((entry) => String(entry.node) === id);
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
			failed: failures,
		});
		const summary = candidates.length
			? `wave ${wave.index} recorded: ${candidates.map((c: any) => c.node).join(", ")}`
			: `wave ${wave.index} recorded no changes`;
		const text = failures.length ? `${summary}\nfailed: ${failures.join("; ")}` : summary;
		// Between waves, deliver any review comments the human wrote.
		relaySafely(ctx, branch);
		// The first recorded commit makes the review client available.
		if (reviewNeeded(state)) void ensureReviewServer(ctx, branch);
		return {
			content: [{ type: "text" as const, text }],
			details: { wave: wave.index, candidates, failures },
			isError: failures.length > 0,
		};
	}

	/**
	 * Verify one or more recorded nodes in a single turn.
	 *
	 * Every node's acceptance vector is submitted to the single executor, one drain
	 * runs the whole batch, and one verifier subagent judges the combined evidence
	 * and returns a per-node verdict. `only` narrows the acceptance checks, so a
	 * re-verify can run a single command.
	 */
	async function verifyNodes(
		ctx: ExtensionContext,
		params: any,
		signal?: AbortSignal,
	): Promise<any> {
		const branch = await featureBranch(ctx);
		if (isPaused(ctx, branch)) return pausedResult("verify");
		const dag = readJson<Dag>(dagPath(ctx.cwd, branch), { nodes: [] });
		const store = stateStore(ctx, branch);
		const state: any = store.read();

		const picked: string[] = Array.isArray(params.nodes) && params.nodes.length
			? params.nodes.map((id: any) => String(id))
			: params.node
				? [String(params.node)]
				: (currentWave(state)?.members ?? []).filter(
						(id) =>
							Boolean(state.nodes[id]?.commit) && nodeStatus(state, id) !== "done",
					);
		const requested: string[] = [...new Set(picked)];
		if (!requested.length) throw new Error("verify: no recorded node in the current wave");
		const only: string[] = Array.isArray(params.only)
			? params.only.map((check: any) => String(check))
			: [];

		const specs: CampaignNode[] = [];
		for (const id of requested) {
			const spec = (dag.nodes ?? []).find((n) => n.id === id);
			if (!spec) throw new Error(`verify: unknown node '${id}'`);
			if (!state.nodes[id]?.commit) {
				throw new Error(`verify: node '${id}' has no recorded commit; run record first`);
			}
			specs.push(spec);
		}

		// The executor is the single runner: enqueue every node's acceptance at its
		// recorded commit, then drain the whole batch once.  A submit whose
		// fingerprint already reached a terminal verdict comes back cached with no
		// queued work (DEC-3).
		const submitted: Array<{ spec: CampaignNode; job: any; cached: boolean }> = [];
		for (const spec of specs) {
			const submitArgs = [
				"exec",
				"--submit",
				"--source",
				`node:${spec.id}`,
				"--commit",
				String(state.nodes[spec.id]?.commit),
				"--gpu",
				spec.gpu ?? "none",
			];
			for (const cmd of spec.acceptance ?? []) submitArgs.push("--command", cmd);
			for (const check of only) submitArgs.push("--only", check);
			const reply = await sliceme(ctx, submitArgs, signal);
			submitted.push({
				spec,
				job: reply.json?.job,
				cached: Boolean(reply.json?.cached),
			});
		}
		// Drain only when a submit queued work; a cached submit already returned a
		// terminal job, so there is nothing to run.
		let drainedById = new Map<string, any>();
		if (submitted.some(({ cached }) => !cached)) {
			const drained = await sliceme(ctx, ["exec", "--run"], signal);
			drainedById = new Map<string, any>(
				(drained.json?.jobs ?? []).map((job: any) => [String(job.id), job]),
			);
		}
		// A cached verdict and a failed job are already decided, so they skip the
		// verifier (DEC-3); only a fresh pass needs an independent judgement.
		const evidence = submitted.map(({ spec, job, cached }) => {
			const current = drainedById.get(String(job?.id)) ?? job;
			const decided =
				cached || current?.status === "failed" || current?.status === "error";
			return { spec, job: current, decided };
		});
		const toVerify = evidence.filter((entry) => !entry.decided);

		const ids = evidence.map(({ spec }) => spec.id);
		let verifierOutput = "";
		const verdicts: Record<string, boolean> = {};
		if (toVerify.length) {
			const described = toVerify
				.map(
					({ spec, job }) =>
						`Node "${spec.id}" (${spec.label ?? ""}) — goal: ${spec.goal ?? ""}.\n` +
						`Acceptance vector: ${(spec.acceptance ?? []).join(" ; ")}. ` +
						`GPU tier: ${spec.gpu ?? "none"}.\n` +
						`Executor status: ${job?.status ?? "unknown"}\n` +
						`Executor fingerprint: ${job?.fingerprint ?? "-"}\n` +
						`${job?.output ?? "(no executor output)"}`,
				)
				.join("\n\n---\n\n");
			const task =
				`Independently verify each DAG node below from the executor's recorded ` +
				`evidence only. You are read-only: do not run commands and do not edit any ` +
				`file. A green command is necessary, not enough: judge whether the evidence ` +
				`actually shows each node's acceptance.\n\n${described}\n\n` +
				`Report one line per node in the form "NODE <id>: PASS" or "NODE <id>: FAIL", ` +
				`then your reasoning grounded in the evidence.`;
			const verifyIds = toVerify.map(({ spec }) => spec.id);
			const result = await runTracked(ctx, branch, {
				agent: "verifier",
				node: verifyIds.join(","),
				unit: `verify:${verifyIds.join(",")}`,
				attempt: Number(state.nodes[verifyIds[0]]?.attempts ?? 1),
				task,
				cwd: ctx.cwd,
				log: path.join(stateDir(ctx.cwd), `${branchKey(branch)}.worker_verify.log`),
				signal,
			});
			if (result.interrupted || isPaused(ctx, branch)) {
				// Suspended mid-verification: leave the nodes `recorded` (state is
				// untouched) so resume re-verifies the same candidates.
				return pausedResult("verify");
			}
			verifierOutput = result.output;
			if (result.exitCode === 0) {
				Object.assign(verdicts, parseVerdicts(result.output, verifyIds));
			} else {
				// A verifier failure is a failed judgement: no node can pass.
				for (const { spec } of toVerify) verdicts[spec.id] = false;
			}
		}

		const verdictById: Record<string, boolean> = {};
		const failed: string[] = [];
		for (const { spec, job, decided } of evidence) {
			const passed = decided
				? job?.status === "passed"
				: job?.status === "passed" && verdicts[spec.id] === true;
			verdictById[spec.id] = passed;
			state.nodes[spec.id] = {
				...(state.nodes[spec.id] ?? {}),
				status: passed ? "done" : "failed",
				verdict: passed ? "pass" : "fail",
				job: job?.id ?? null,
				...(passed
					? {}
					: { lastError: decided ? job?.output : verifierOutput || job?.output }),
			};
			if (!passed) failed.push(spec.id);
		}
		const completed = refreshWaves(state);
		store.save();
		widget(ctx, dag, state);
		logEvent(ctx.cwd, branch, "node.verdict", {
			nodes: ids,
			failed,
			completed_waves: completed.map((w) => w.index),
		});

		// Nothing lands on the target per wave.  When every wave is done and every
		// node passed, try the delivery; the engine refuses until the human approves.
		let delivery: any = null;
		if (!failed.length && allWavesDone(state)) {
			delivery = await tryDelivery(ctx, branch, state, signal);
		}
		const summary = ids.map((id) => `${id}: ${verdictById[id] ? "PASS" : "FAIL"}`).join("\n");
		return {
			content: [
				{
					type: "text" as const,
					text: verifierOutput ? `${summary}\n${verifierOutput}` : summary,
				},
			],
			details: { nodes: ids, failed, verdicts: verdictById, delivery: delivery?.json ?? null },
			isError: failed.length > 0,
		};
	}

	function allWavesDone(state: CampaignState): boolean {
		const waves = state.waves ?? [];
		return waves.length > 0 && waves.every((w) => w.status === "done");
	}

	/**
	 * Push the campaign worktree branch and open the pull request once every wave
	 * is done and every commit is approved.  The human approves commits in the
	 * review client; the engine refuses delivery until then.  A refusal is not an
	 * error: the review relay retries after the next approval.
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
			/* the report is evidence for the reviewer, never a delivery gate */
		}
		const cleanup = cleanupOverride !== undefined ? String(cleanupOverride) : "none";
		const args = ["deliver", "--target", target];
		if (cleanup !== "none") args.push("--cleanup", cleanup);
		let delivered: any;
		try {
			delivered = await sliceme(ctx, args, signal);
		} catch (error) {
			const message = String((error as Error)?.message ?? error);
			// Not approved, or a comment is delivered but not addressed: both are
			// transient gates; stay ready for the next review poll.
			if (!message.includes("not-approved") && !message.includes("unaddressed-comments")) {
				throw error;
			}
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
			// `cleanup: all` also removes the descriptor, so do not write it back.
			if (cleanup !== "all") {
				writeSessionDescriptor(
					ctx,
					branch,
					{ status: "completed", reason: "delivered" },
					state,
				);
			}
			// Delivery merges every approved commit; the review surface is done.
			stopReviewServer(ctx, branch);
		} else {
			store.save();
		}
		logEvent(ctx.cwd, branch, failed ? "campaign.deliver_failed" : "campaign.delivered", {
			target,
			source,
			pull_request: delivered.json?.pull_request ?? null,
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
		const dag = readJson<Dag>(dagPath(ctx.cwd, branch), { nodes: [] });
		let nextHint = "";
		if (dag?.design) {
			const plan = parseCampaignPlan(readDesignFile(ctx, dag.design));
			const current = plan.findIndex((entry) => entry.target === branch);
			const next = plan.find(
				(entry, index) =>
					index > current && !fs.existsSync(statePath(ctx.cwd, entry.target)),
			);
			if (next) {
				nextHint =
					`\nNext campaign in the plan: '${next.name}' (target ${next.target}). ` +
					`Run start --design ${dag.design} --campaign ${next.name}.`;
			}
		}
		return {
			content: [{ type: "text" as const, text: `${delivered.text}${nextHint}` }],
			details: delivered.json ?? {},
		};
	}

	pi.registerTool({
		name: "sliceme",
		label: "Sliceme",
		description:
			"Coordinate a design into landed work: start (choose target branch + planner), " +
			"status, ready, spawn (one call fans a wave out to one-shot editors in the campaign " +
			"worktree), record (commit the wave), verify (executor runs; one verifier turn judges " +
			"the wave), deliver (merge to the target after approval), report, exec (sandbox gate, " +
			"campaign worktree, check queue). The dag.json plan is the only schedule; waves are a " +
			"projection of it.",
		promptSnippet: "Drive an Sliceme campaign (start → spawn → record → verify → deliver)",
		promptGuidelines: [
			"Drive the campaign continuously. After `start` returns the DAG, call `ready` and " +
				"`spawn` the current wave in the same turn, then record, verify, and advance to the " +
				"next wave. Do not stop to ask for permission between steps. Pause only at the " +
				"human gates: the target-branch choice, an explicit user suspension, a failed " +
				"sandbox gate, and the final commit review before delivery.",
			"The target (feature) branch is chosen once at start and is never main, master, or " +
				"the repository default branch. There is no override; refuse and re-choose instead.",
			"The DAG in dag.json is the only authored schedule; waves are its deterministic " +
				"projection (owns + depends_on, capped by concurrency). Merge nodes that share an " +
				"owned directory and sit on one dependency chain into a single node.",
			"Workers are pure editors in the one shared campaign worktree: they never run git.",
			"A node starts once every dependency is done (readiness is the gate); the wave is a " +
				"display hint. Never recreate the worktree or rebase between waves.",
			"Spawn a wave with one call: `spawn --nodes <id,id,...>` (or `spawn` for every ready " +
				"node of the current wave) fans the workers out concurrently and opens the worktree " +
				"once; never exceed the wave cap.",
			"After all workers in the current wave finish editing, call `record` to commit the " +
				"wave onto the campaign worktree (per-node commits, ownership conformance).",
			"A node is ready only once every dependency is done, never merely verified.",
			"Judge a wave with one `verify` turn: `verify --nodes <id,id,...>` (or `verify` for " +
				"the current wave) submits every node's acceptance, drains the whole batch once, and " +
				"runs a single verifier. Pass `--only <check>` to re-verify one command.",
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
			node: Type.Optional(Type.String({ description: "spawn/verify: a single node id" })),
			nodes: Type.Optional(
				Type.Array(Type.String(), {
					description:
						"spawn/verify: node ids; one call fans a wave out or judges it in one turn",
				}),
			),
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
			commits: Type.Optional(
				Type.Array(Type.String(), { description: "exec: commit refs to run as one batch" }),
			),
			only: Type.Optional(
				Type.Array(Type.String(), {
					description: "exec/verify: keep only these checks (matched by name or command)",
				}),
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
			reply: Type.Optional(
				Type.Boolean({ description: "review: record a reply row for a comment" }),
			),
			addressed: Type.Optional(
				Type.Boolean({ description: "review: mark a root comment addressed" }),
			),
			resolve: Type.Optional(
				Type.Boolean({ description: "review: resolve one comment to a node" }),
			),
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
			parent_comment_id: Type.Optional(
				Type.Number({ description: "review: replied-to root comment id" }),
			),
			addressing_commit: Type.Optional(
				Type.String({ description: "review: commit that answers a comment" }),
			),
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
				case "plan": {
					const design = String(params.design ?? "DESIGN.md");
					const { json, text } = await sliceme(ctx, ["plan", "--design", design], signal);
					return { content: [{ type: "text" as const, text }], details: json ?? {} };
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
					return spawnNodes(ctx, params, signal, onUpdate);
				case "record":
					return recordWave(ctx, params, signal);
				case "verify":
					return verifyNodes(ctx, params, signal);
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
				case "review":
				case "progress": {
					const keys =
						params.action === "exec"
							? EXEC_KEYS
							: params.action === "wave"
								? WAVE_KEYS
								: params.action === "progress"
									? PROGRESS_KEYS
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
			const branch = activeCampaign(ctx.cwd);
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
			const branch = activeCampaign(ctx.cwd);
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
			const branch = activeCampaign(ctx.cwd);
			if (branch) {
				const { state } = load(ctx, branch);
				if (reviewNeeded(state)) void ensureReviewServer(ctx, branch);
				relaySafely(ctx, branch);
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
			const branch = activeCampaign(ctx.cwd);
			clearActiveCampaign(ctx.cwd);
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
