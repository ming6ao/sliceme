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
 * the model to start a campaign (defaulting to ``DESIGN.md``).  There is no
 * separate skill.
 *
 * Install as part of the `sliceme` pi package (`pi install ./` or
 * `pi install npm:sliceme`); shared helpers live in `./common.ts`.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import {
	branchKey,
	controlPath,
	dagPath,
	heartbeatPath,
	logEvent,
	logPath,
	readJson,
	runSliceme,
	runSubagent,
	sessionPath,
	stateDir,
	statePath,
	writeJson,
} from "./common.ts";
import type { SubagentResult } from "./common.ts";

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
] as const;

/** Parameter names the `exec` action forwards to the engine verb. */
const EXEC_KEYS = [
	"validate",
	"gpu_required",
	"open",
	"record",
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
	"message",
	"summary",
] as const;

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
	status: "pending" | "running" | "recorded" | "done" | "failed" | "stopped";
	attempts?: number;
	verdict?: string;
	lastError?: string;
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
		const integrated = members.filter((id) => state.nodes[id]?.status === "done");
		const running = members.some((id) => state.nodes[id]?.status === "running");
		const status: WaveState["status"] =
			members.length > 0 && integrated.length === members.length
				? "done"
				: running
					? "running"
					: "pending";
		return {
			index: Number(dw.wave),
			members,
			status,
			integrated,
			cleanup_done: prev?.cleanup_done ?? false,
		};
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

/** Mark any wave whose members are all done as done. */
function advanceWaves(state: CampaignState): WaveState[] {
	const completed: WaveState[] = [];
	for (const wave of state.waves ?? []) {
		wave.integrated = wave.members.filter((id) => state.nodes[id]?.status === "done");
		const allDone =
			wave.members.length > 0 && wave.integrated.length === wave.members.length;
		if (allDone && wave.status !== "done") {
			wave.status = "done";
			completed.push(wave);
		}
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

export default function coordinatorExtension(pi: ExtensionAPI) {
	// Extension-only entry point. The tools register inactive; `/sliceme
	// [DESIGN.md]` activates them and asks the model to start a campaign. No
	// design document is required up front: `start` fails loudly if the path is
	// wrong.
	pi.registerCommand("sliceme", {
		description: "Start a Sliceme campaign from a design document (default DESIGN.md)",
		handler: async (args, ctx) => {
			const design = args.trim() || "DESIGN.md";
			if (!ctx.isIdle()) {
				ctx.ui.notify("sliceme: the agent is busy; finish the current turn first.", "warning");
				return;
			}
			const active = new Set(pi.getActiveTools());
			active.add("sliceme");
			active.add("sliceme-unit");
			pi.setActiveTools([...active]);
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
			state: readJson<CampaignState>(statePath(ctx.cwd, branch), { nodes: {} }),
		};
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
	): any {
		const { dag, state } = load(ctx, branch);
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
	): any {
		const descriptor = buildSessionDescriptor(ctx, branch, over);
		writeJson(sessionPath(ctx.cwd, branch), descriptor);
		return descriptor;
	}

	function resumePrompt(branch: string, descriptor: any): string {
		const campaign = descriptor?.campaign ?? branch;
		const recordWave = descriptor?.resume_plan?.record_wave;
		return (
			`Resume the suspended Sliceme campaign "${campaign}" on branch "${branch}". ` +
			`Use the sliceme tool: action "status" to see the plan, then continue the ` +
			`current wave (spawn ready nodes, record, verify). ` +
			(recordWave !== undefined && recordWave !== null
				? `A wave record is pending: run exec --record --wave ${recordWave} first. `
				: "") +
			`Do not restart completed nodes.`
		);
	}

	/** Run a subagent with attempt bookkeeping and a per-node heartbeat. */
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
		},
	): Promise<SubagentResult> {
		const attempt = opts.attempt ?? 1;
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
		});
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
					`tools ${heartbeat.toolCalls ?? 0}, last tool "${heartbeat.last_tool ?? "?"}".`,
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
		writeJson(statePath(ctx.cwd, branch), state);
		logEvent(ctx.cwd, branch, "wave.replanned", {
			waves: (state.waves ?? []).map((w) => w.members),
		});
	}

	function widget(ctx: ExtensionContext, dag: Dag, state: CampaignState): void {
		if (!ctx.hasUI) return;
		const marker = (status: string) =>
			status === "running" ? "●" : status === "done" ? "✓" : status === "failed" ? "✗" : "·";
		const lines: string[] = [];
		for (const wave of state.waves ?? []) {
			const members = wave.members.map((id) => {
				const node = (dag.nodes ?? []).find((n) => n.id === id);
				const status = nodeStatus(state, id);
				return `${marker(status)} ${id}${node?.label ? ` ${node.label}` : ""}`.trim();
			});
			lines.push(`─ wave ${wave.index} [${wave.status}]  ${members.join("   ")}`);
		}
		if (!lines.length) {
			lines.push(
				...nodeIds(dag).map((id) => {
					const status = nodeStatus(state, id);
					return `${marker(status)} ${id} [${status}]`;
				}),
			);
		}
		ctx.ui.setWidget("sliceme", lines.length ? lines : ["sliceme: no plan"]);
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
		stateFile: string,
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
		writeJson(stateFile, state);
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
		const opened = await sliceme(ctx, ["exec", "--open"], signal);
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
		const stateFile = statePath(ctx.cwd, branch);

		// 2a. Resume: rebuild node status from git/state.db, which always win
		// over state.json. A node left `running` by a crash is reset.
		if (fs.existsSync(dagFile) && !params.replan) {
			const existing = readJson<Dag>(dagFile, { nodes: [] });
			if (existing?.nodes?.length) {
				const state: any = readJson(stateFile, { nodes: {} });
				const status = (await sliceme(ctx, ["status"], signal)).json;
				const candidates = status?.candidates ?? [];
				// Consult the engine's resume plan so a worker interrupted with
				// edits preserved in the shared worktree is continued, not restarted.
				let resumePlan: any = null;
				if (fs.existsSync(sessionPath(ctx.cwd, branch))) {
					try {
						// Read the plan before --open recreates a hand-deleted worktree,
						// so a lost worktree still maps to a fresh spawn, not a pause.
						resumePlan = (await sliceme(ctx, ["resume", "--plan-only"], signal)).json;
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
				writeJson(stateFile, state);
				await ensureWaves(ctx, branch, existing, state);
				const pendingWave = resumePlan?.resume_plan?.record_wave;
				if (
					pendingWave !== undefined &&
					pendingWave !== null &&
					resumePlan?.worktree_dirty
				) {
					try {
						await sliceme(
							ctx,
							["exec", "--record", "--wave", String(pendingWave)],
							signal,
						);
						logEvent(ctx.cwd, branch, "wave.record_on_resume", { wave: pendingWave });
					} catch {
						/* unowned or ambiguous edits: leave the record for a human/CLI */
					}
				}
				const resumeGateError = await sandboxGate(ctx, branch, state, stateFile, signal);
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
			`node every touched component depends_on; each node lists its acceptance commands; ` +
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
		writeJson(stateFile, state);
		await ensureWaves(ctx, branch, dag, state);
		const gateError = await sandboxGate(ctx, branch, state, stateFile, signal);
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
	): Promise<any> {
		const node = String(params.node ?? "");
		if (!node) throw new Error("spawn requires --node <id>");
		const { json } = await sliceme(ctx, ["status"]);
		const branch = String(json?.feature_branch ?? "main");
		const stateFile = statePath(ctx.cwd, branch);
		const dag = readJson<Dag>(dagPath(ctx.cwd, branch), { nodes: [] });
		const state: any = readJson(stateFile, { nodes: {} });
		if (isPaused(ctx, branch)) return pausedResult("spawn");
		const spec = (dag.nodes ?? []).find((n) => n.id === node);
		if (!spec) throw new Error(`spawn: unknown node '${node}'`);
		await ensureWaves(ctx, branch, dag, state);

		const maxAttempts = Number(dag.max_attempts ?? 3);
		const attempts = Number(state.nodes[node]?.attempts ?? 0);
		if (attempts >= maxAttempts) {
			state.nodes[node] = { ...(state.nodes[node] ?? {}), status: "failed" };
			writeJson(stateFile, state);
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
		writeJson(stateFile, state);
		logEvent(ctx.cwd, branch, "node.spawn", { node, unit: unit.name, attempt });

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
		});
		if (result.interrupted || isPaused(ctx, branch)) {
			// A suspended worker keeps its edits in the shared campaign worktree; mark
			// the node paused so resume continues it instead of respawning from scratch.
			// Its attempt is not consumed: the next spawn reuses the same attempt number.
			state.nodes[node].status = "paused";
			writeJson(stateFile, state);
			logEvent(ctx.cwd, branch, "node.suspended", {
				node,
				attempt,
				signal: result.signal ?? null,
			});
			return pausedResult("spawn");
		}
		state.nodes[node].status = result.exitCode === 0 ? "pending" : "failed";
		state.nodes[node].attempts = attempt;
		writeJson(stateFile, state);
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
		const stateFile = statePath(ctx.cwd, branch);
		const dag = readJson<Dag>(dagPath(ctx.cwd, branch), { nodes: [] });
		const state: any = readJson(stateFile, { nodes: {} });
		await ensureWaves(ctx, branch, dag, state);
		const wave = currentWave(state);
		if (!wave) throw new Error("record: no open wave to record");
		const recorded = await sliceme(
			ctx,
			["exec", "--record", "--wave", String(wave.index)],
			signal,
		);
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
		writeJson(stateFile, state);
		logEvent(ctx.cwd, branch, "wave.recorded", {
			wave: wave.index,
			candidates: candidates.map((c: any) => ({ node: c.node, commit: c.head_commit })),
		});
		const summary = candidates.length
			? `wave ${wave.index} recorded: ${candidates.map((c: any) => c.node).join(", ")}`
			: `wave ${wave.index} recorded no changes`;
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
		const stateFile = statePath(ctx.cwd, branch);
		const state: any = readJson(stateFile, { nodes: {} });
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
		const completed = advanceWaves(state);
		writeJson(stateFile, state);
		logEvent(ctx.cwd, branch, "node.verdict", {
			node,
			passed,
			gpu: spec.gpu ?? "none",
			job: job?.id ?? null,
			completed_waves: completed.map((w) => w.index),
		});

		// Nothing is merged per wave.  Only when every wave is done do we ask the
		// user once for approval to merge the campaign worktree into the target.
		let delivery: any = null;
		if (passed && allWavesDone(state)) {
			delivery = await offerDelivery(ctx, branch, state, stateFile, signal);
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
	 * The one approval gate: when every wave is done, ask the user whether to merge
	 * the campaign worktree into the target branch, then run `deliver`.  In
	 * non-interactive modes this records `ready_to_deliver` for a later `deliver`.
	 */
	async function offerDelivery(
		ctx: ExtensionContext,
		branch: string,
		state: any,
		stateFile: string,
		signal?: AbortSignal,
	): Promise<any> {
		if (state.delivered) return null;
		const target = String(state.target_branch ?? branch);
		const source = String(state.worktree_branch ?? "");
		if (!ctx.hasUI) {
			state.ready_to_deliver = true;
			writeJson(stateFile, state);
			return null;
		}
		const ok = await ctx.ui.confirm(
			`All waves are done. Merge the campaign worktree into '${target}'?`,
			`Source branch: ${source || "(campaign worktree)"}. This runs the trusted checks ` +
				`and merges with --no-ff. The target is never the default branch.`,
		);
		if (!ok) {
			state.ready_to_deliver = true;
			writeJson(stateFile, state);
			return null;
		}
		const delivered = await sliceme(ctx, ["deliver", "--target", target], signal);
		const failed = (delivered.json?.results ?? []).some((r: any) => r.status === "failed");
		state.ready_to_deliver = false;
		if (!failed) state.delivered = true;
		writeJson(stateFile, state);
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
		const stateFile = statePath(ctx.cwd, branch);
		const state: any = readJson(stateFile, { nodes: {} });
		const delivered = await offerDelivery(ctx, branch, state, stateFile, signal);
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
				"projection (owns + depends_on, capped by concurrency).",
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
			"Use `exec` for the sandbox gate (--validate), the campaign worktree (--open), and " +
				"the check queue (--submit/--run/--wait).",
			"Do NOT merge to the target per wave. Only when every wave is done does `deliver` " +
				"ask the user once for approval, then merge the campaign worktree with --no-ff.",
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
				Type.Boolean({ description: "exec: create/reuse the campaign worktree" }),
			),
			record: Type.Optional(
				Type.Boolean({ description: "exec: record a wave (conformance + per-node commits)" }),
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
			priority: Type.Optional(Type.Number({ description: "exec: higher runs first" })),
			timeout: Type.Optional(Type.Number({ description: "exec: timeout seconds" })),
			wave: Type.Optional(Type.Number({ description: "exec: wave index" })),
			requester: Type.Optional(Type.String({ description: "exec: verifier id" })),
			limit: Type.Optional(Type.Number({ description: "exec: max jobs to drain" })),
			message: Type.Optional(Type.String({ description: "exec record: commit message" })),
			summary: Type.Optional(Type.String({ description: "exec record: candidate summary" })),
		}),
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			switch (params.action as (typeof CAMPAIGN_ACTIONS)[number]) {
				case "start":
					return startCampaign(ctx, params, signal);
				case "status": {
					const branch = await featureBranch(ctx);
					const { dag, state } = load(ctx, branch);
					await ensureWaves(ctx, branch, dag, state);
					widget(ctx, dag, state);
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
					return spawnNode(ctx, params, signal);
				case "record":
					return recordWave(ctx, params, signal);
				case "verify":
					return verifyNode(ctx, params, signal);
				case "deliver":
					return deliverCampaign(ctx, params, signal);
				case "report": {
					const branch = await featureBranch(ctx);
					const { dag } = load(ctx, branch);
					const args = ["report"];
					if (params.narrative) args.push("--narrative", String(params.narrative));
					if (dag.design) args.push("--design", dag.design);
					const { json, text } = await sliceme(ctx, args, signal);
					return { content: [{ type: "text" as const, text }], details: json ?? {} };
				}
				case "exec": {
					const args = ["exec"];
					for (const key of EXEC_KEYS) {
						const value = (params as Record<string, unknown>)[key];
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
					const { json, text } = await sliceme(ctx, args, signal);
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
		try {
			if (event.reason !== "resume" && event.reason !== "startup") return;
			const branch = configuredBranch(ctx.cwd);
			if (!branch) return;
			const descriptor = readJson<any>(sessionPath(ctx.cwd, branch), undefined);
			if (!descriptor || descriptor.status !== "suspended") return;
			clearPause(ctx, branch);
			const text = resumePrompt(branch, descriptor);
			// Mark it active so a reload/restart does not inject the prompt twice.
			writeJson(sessionPath(ctx.cwd, branch), {
				...descriptor,
				status: "active",
				resumed_at: Date.now() / 1000,
			});
			if (event.reason === "resume") {
				pi.sendUserMessage(text);
				return;
			}
			if (!ctx.hasUI || !ctx.isIdle()) return;
			const ok = await ctx.ui.confirm("Resume Sliceme campaign?", text);
			if (ok) pi.sendUserMessage(text);
		} catch {
			/* a resume hook must never break session startup */
		}
	});

	pi.on("session_shutdown", (event, ctx) => {
		// Fast, idempotent, and subprocess-free: only the descriptor is written.
		try {
			const branch = configuredBranch(ctx.cwd);
			if (!branch || !fs.existsSync(dagPath(ctx.cwd, branch))) return;
			const { state } = load(ctx, branch);
			writeSessionDescriptor(ctx, branch, {
				status: state.delivered ? "completed" : "suspended",
				reason: event.reason === "reload" ? "reload" : "user",
			});
		} catch {
			/* never block shutdown */
		}
	});
}

export { nodeIds, nodeStatus, readyNodes, summarise };
