/**
 * Shared helpers for the Sliceme pi extensions.
 *
 * `coordinator.ts` is the `sliceme` coordinator tool and `unit.ts` is the
 * `sliceme-unit` tool; both are thin adapters over the bundled `sliceme` CLI. The CLI is the
 * engine surface, so the tools stay harness-agnostic and need no `PATH`
 * install. `runSubagent` also applies each agent's `tools:` allowlist, scoping
 * workers to the unit tool and the coordinator to the campaign tool.
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

export interface SlicemeResult {
	text: string;
	json: any;
}

export interface SubagentResult {
	exitCode: number;
	output: string;
	stderr: string;
	/** True when the child was killed by a signal rather than exiting on its own. */
	interrupted?: boolean;
	signal?: NodeJS.Signals | null;
}

export interface SlicemeInvocation {
	command: string;
	prefix: string[];
}

const HERE = (() => {
	try {
		return path.dirname(fileURLToPath(import.meta.url));
	} catch {
		return process.cwd();
	}
})();

/** The package root (the directory containing `bin/`, `integrations/`, `docs/`). */
export function packageDir(): string {
	let dir = HERE;
	for (let i = 0; i < 6; i++) {
		if (existsSync(path.join(dir, "bin", "sliceme"))) return dir;
		const parent = path.dirname(dir);
		if (parent === dir) break;
		dir = parent;
	}
	return path.resolve(HERE, "..", "..");
}

/**
 * Resolve how to run the bundled CLI. `SLICEME_BIN` overrides for local
development; otherwise the CLI shipped inside the pi package is used, so no
`PATH` install is needed. Running the bundled script through `python3` keeps it
portable.
 */
export function resolveSlicemeInvocation(): SlicemeInvocation {
	if (process.env.SLICEME_BIN) {
		return { command: process.env.SLICEME_BIN, prefix: [] };
	}
	const bundled = path.join(packageDir(), "bin", "sliceme");
	return { command: "python3", prefix: [bundled] };
}

export function parseJson(text: string): any {
	try {
		return JSON.parse(text);
	} catch {
		return undefined;
	}
}

/** Run the Sliceme CLI with `--json` from the session's cwd. */
export async function runSliceme(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	args: string[],
	signal?: AbortSignal,
	timeout = 600_000,
): Promise<SlicemeResult> {
	const invocation = resolveSlicemeInvocation();
	const result = await pi.exec(invocation.command, [...invocation.prefix, "--json", ...args], {
		cwd: ctx.cwd,
		signal,
		timeout,
	});
	const text = [result.stdout, result.stderr].filter((s) => s?.trim()).join("\n").trim();
	if (result.code !== 0) {
		throw new Error(text || `sliceme exited with code ${result.code}`);
	}
	return { text: text || "ok", json: parseJson(result.stdout) };
}

export interface ReviewServerHandle {
	child: import("node:child_process").ChildProcess;
	url: string;
}

/**
 * Start the review server as a background child and wait for its URL file.
 *
 * The server binds loopback and writes the URL to a private file with mode
 * `0600`. The child is not detached, so a coordinator crash stops the server.
 * The child also holds a pipe on standard input, so a crashed coordinator
 * closes the pipe and the server stops itself. The caller stops the child in
 * `session_shutdown`. The server opens the default browser by itself; the
 * returned URL feeds the coordinator widget.
 */
export async function spawnReviewServer(options: {
	cwd: string;
	urlFile: string;
	logFile: string;
	waitMs?: number;
}): Promise<ReviewServerHandle> {
	const invocation = resolveSlicemeInvocation();
	fs.mkdirSync(path.dirname(options.logFile), { recursive: true });
	// Remove URL files from earlier sessions that are no longer running.
	cleanStaleReviewUrls(path.dirname(options.urlFile));
	// A leftover URL file from an earlier run must not be read as the new URL.
	fs.rmSync(options.urlFile, { force: true });
	// Capture stderr only. The server prints the URL, which holds the write
	// token, on stdout, so the token never enters the log. Mode 0600 keeps the
	// log private when the operating system creates it.
	const err = fs.openSync(options.logFile, "a", 0o600);
	const child = spawn(
		invocation.command,
		[...invocation.prefix, "review", "--serve", "--url-file", options.urlFile],
		{ cwd: options.cwd, shell: false, detached: false, stdio: ["pipe", "ignore", err] },
	);
	fs.closeSync(err);
	const deadline = Date.now() + (options.waitMs ?? 5000);
	try {
		while (Date.now() < deadline) {
			if (child.exitCode !== null) {
				throw new Error("the review server exited before it wrote a URL");
			}
			try {
				const url = fs.readFileSync(options.urlFile, "utf8").trim();
				if (url) return { child, url };
			} catch {
				/* the server has not written the file yet */
			}
			await new Promise((resolve) => setTimeout(resolve, 100));
		}
		throw new Error("the review server did not write a URL in time");
	} catch (error) {
		child.kill("SIGTERM");
		throw error;
	}
}

// ---------------------------------------------------------------------------
// Campaign state paths (`docs/reference.md` §3)
// ---------------------------------------------------------------------------

export function branchKey(branch: string): string {
	return (branch || "main").trim().replace(/\//g, "--") || "main";
}

export function stateDir(cwd: string): string {
	return path.join(cwd, ".sliceme");
}

export function dagPath(cwd: string, branch: string): string {
	return path.join(stateDir(cwd), `${branchKey(branch)}.dag.json`);
}

export function statePath(cwd: string, branch: string): string {
	return path.join(stateDir(cwd), `${branchKey(branch)}.state.json`);
}

export function logPath(cwd: string, branch: string, node: string): string {
	return path.join(stateDir(cwd), `${branchKey(branch)}.worker_${node}.log`);
}

export function eventsPath(cwd: string, branch: string): string {
	return path.join(stateDir(cwd), `${branchKey(branch)}.events.jsonl`);
}

/** The adapter-written suspend/resume descriptor (`docs/sessions.md`). */
export function sessionPath(cwd: string, branch: string): string {
	return path.join(stateDir(cwd), `${branchKey(branch)}.session.json`);
}

/** The cooperative pause flag for a campaign. */
export function controlPath(cwd: string, branch: string): string {
	return path.join(stateDir(cwd), `${branchKey(branch)}.control.json`);
}

/** The per-node progress heartbeat written while a subagent runs. */
export function heartbeatPath(cwd: string, branch: string, node: string): string {
	return path.join(stateDir(cwd), `${branchKey(branch)}.progress_${node}.json`);
}

/**
 * The private file that holds the running review server URL (mode 0600).  The
 * name carries the coordinator process id, so two sessions in one checkout do
 * not overwrite each other's URL.
 */
export function reviewUrlPath(cwd: string, pid: number = process.pid): string {
	return path.join(stateDir(cwd), `review.${pid}.url`);
}

/** Whether a process with this id exists. */
function processAlive(pid: number): boolean {
	try {
		process.kill(pid, 0);
		return true;
	} catch (error) {
		return (error as NodeJS.ErrnoException).code === "EPERM";
	}
}

/** Remove review URL files whose owning coordinator is gone. */
export function cleanStaleReviewUrls(cwd: string): void {
	const dir = stateDir(cwd);
	let names: string[];
	try {
		names = fs.readdirSync(dir);
	} catch {
		return;
	}
	for (const name of names) {
		const match = /^review\.(\d+)\.url$/.exec(name);
		if (!match) continue;
		const pid = Number(match[1]);
		if (pid === process.pid || processAlive(pid)) continue;
		try {
			fs.rmSync(path.join(dir, name), { force: true });
		} catch {
			/* a concurrent cleanup may win the race */
		}
	}
}

/** The review server's standard output log. */
export function reviewLogPath(cwd: string): string {
	return path.join(stateDir(cwd), "review.server.log");
}

export function readJson<T>(file: string, fallback: T): T {
	try {
		return JSON.parse(fs.readFileSync(file, "utf8")) as T;
	} catch {
		return fallback;
	}
}

/** Write a JSON file atomically: a sibling temporary file, then a rename. */
export function writeJson(file: string, data: unknown): void {
	fs.mkdirSync(path.dirname(file), { recursive: true });
	const tmp = `${file}.${process.pid}.${Date.now()}.tmp`;
	fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + "\n", "utf8");
	fs.renameSync(tmp, file);
}

/**
 * The single in-process owner of one campaign's `state.json`.
 *
 * The coordinator is the only writer, so the store keeps the state in memory
 * and hands every caller the same object. Mutating that one object and calling
 * `save()` therefore never drops a parallel `spawn` completion, and the atomic
 * write never leaves a partial file.
 */
export class CampaignStateStore {
	private readonly file: string;
	private cache: any;

	constructor(file: string) {
		this.file = file;
	}

	/** The shared state object, loaded from disk on first use. */
	read<T = any>(): T {
		if (this.cache === undefined) this.cache = readJson<any>(this.file, { nodes: {} });
		return this.cache as T;
	}

	/** Read the file again, for a resume rebuild. */
	reload<T = any>(): T {
		this.cache = undefined;
		return this.read<T>();
	}

	/** Replace the shared state, for a new campaign. */
	replace(state: any): void {
		this.cache = state;
	}

	/** Flush the shared state. `write` is false when the engine removed the file. */
	save(write = true): void {
		if (write) writeJson(this.file, this.read());
	}
}

/**
 * Append one audit line to the campaign event log. The DAG can be hand-edited,
 * so plan evolution is recorded here next to commits and fingerprints.
 */
export function logEvent(cwd: string, branch: string, kind: string, data: unknown): void {
	const file = eventsPath(cwd, branch);
	fs.mkdirSync(path.dirname(file), { recursive: true });
	fs.appendFileSync(file, JSON.stringify({ kind, data }) + "\n", "utf8");
}

// ---------------------------------------------------------------------------
// Subagents
// ---------------------------------------------------------------------------

function getPiInvocation(args: string[]): { command: string; args: string[] } {
	const currentScript = process.argv[1];
	const isBunVirtualScript = currentScript?.startsWith("/$bunfs/root/");
	if (currentScript && !isBunVirtualScript && fs.existsSync(currentScript)) {
		return { command: process.execPath, args: [currentScript, ...args] };
	}
	const execName = path.basename(process.execPath).toLowerCase();
	if (!/^(node|bun)(\.exe)?$/.test(execName)) {
		return { command: process.execPath, args };
	}
	return { command: "pi", args };
}

function agentDir(): string {
	return process.env.PI_AGENT_DIR || path.join(os.homedir(), ".pi", "agent");
}

/** Locate an agent definition: packaged first, then the user's campaign-agents. */
export function findAgentFile(name: string): string | undefined {
	for (const file of [
		path.join(packageDir(), "integrations", "pi", "agents", `${name}.md`),
		path.join(agentDir(), "campaign-agents", `${name}.md`),
	]) {
		if (fs.existsSync(file)) return file;
	}
	return undefined;
}

/** Read a scalar `key: value` from an agent file's YAML frontmatter. */
function agentFrontmatterValue(raw: string, key: string): string | undefined {
	const match = raw.match(/^---\r?\n([\s\S]*?)\r?\n---/);
	if (!match) return undefined;
	for (const line of match[1].split(/\r?\n/)) {
		const idx = line.indexOf(":");
		if (idx === -1) continue;
		if (line.slice(0, idx).trim() === key) return line.slice(idx + 1).trim();
	}
	return undefined;
}

/**
 * Spawn a one-shot `pi` subagent, tee its raw output to `log`, return the final
 * assistant text. The child is a direct child of the coordinator and is not
 * detached, so a coordinator crash kills it.
 *
 * When `heartbeat` is set, a small progress snapshot is written atomically at
 * most once per second (and once more on close) so a resumed campaign can
 * describe what the paused worker was doing.
 */
export interface SubagentProgress {
	node?: string;
	unit?: string;
	attempt?: number;
	agent?: string;
	turns: number;
	toolCalls: number;
	tools: Record<string, number>;
	lastTool?: string;
	lastToolArgs?: string;
	lastText?: string;
	tokensIn: number;
	tokensOut: number;
	cost: number;
	startedAt: number;
	updatedAt: number;
}

export async function runSubagent(options: {
	agent: string;
	task: string;
	cwd: string;
	log?: string;
	signal?: AbortSignal;
	node?: string;
	unit?: string;
	attempt?: number;
	heartbeat?: string;
	onProgress?: (progress: SubagentProgress) => void;
}): Promise<SubagentResult> {
	const agentFile = findAgentFile(options.agent);
	const args = ["--mode", "json", "-p", "--no-session"];
	let promptPath: string | undefined;
	if (agentFile) {
		const raw = fs.readFileSync(agentFile, "utf8");
		// Scope the subagent to its frontmatter `tools:`. `--tools` replaces the
		// default selection, so the list must name every tool the agent needs;
		// this is what gives workers the `sliceme` unit tool but never `campaign`.
		const tools = agentFrontmatterValue(raw, "tools");
		if (tools) args.push("--tools", tools);
		// Strip the YAML frontmatter before appending the system prompt.
		const stripped = raw.replace(/^---\n[\s\S]*?\n---\n/, "");
		promptPath = path.join(
			os.tmpdir(),
			`sliceme-campaign-${options.agent}-${process.pid}-${Date.now()}.md`,
		);
		fs.writeFileSync(promptPath, stripped, "utf8");
		args.push("--append-system-prompt", promptPath);
	}
	args.push(options.task);

	const cleanup = () => {
		if (!promptPath) return;
		try {
			fs.unlinkSync(promptPath);
		} catch {
			/* ignore */
		}
	};

	const invocation = getPiInvocation(args);
	return new Promise<SubagentResult>((resolve) => {
		const proc = spawn(invocation.command, invocation.args, {
			cwd: options.cwd,
			shell: false,
			stdio: ["ignore", "pipe", "pipe"],
		});
		let buffer = "";
		let output = "";
		let stderr = "";
		let stream: fs.WriteStream | undefined;
		if (options.log) {
			fs.mkdirSync(path.dirname(options.log), { recursive: true });
			stream = fs.createWriteStream(options.log, { flags: "a" });
		}

		const nowSeconds = () => Date.now() / 1000;
		const progress: SubagentProgress = {
			node: options.node,
			unit: options.unit,
			attempt: options.attempt ?? 1,
			agent: options.agent,
			turns: 0,
			toolCalls: 0,
			tools: {},
			tokensIn: 0,
			tokensOut: 0,
			cost: 0,
			startedAt: nowSeconds(),
			updatedAt: nowSeconds(),
		};
		let pendingUsage: any;
		let lastHeartbeatWrite = 0;
		const flushHeartbeat = (force = false) => {
			if (!options.heartbeat) return;
			const now = nowSeconds();
			if (!force && now - lastHeartbeatWrite < 1) return;
			lastHeartbeatWrite = now;
			const snapshot: SubagentProgress = {
				...progress,
				tools: { ...progress.tools },
				updatedAt: now,
			};
			// On disk the heartbeat uses the `.sliceme/` snake_case convention so the
			// engine projection and the continuation prompt can read it.
			const record = {
				node: snapshot.node,
				unit: snapshot.unit,
				attempt: snapshot.attempt ?? 1,
				agent: snapshot.agent,
				pid: process.pid,
				started_at: snapshot.startedAt,
				updated_at: snapshot.updatedAt,
				turns: snapshot.turns,
				tool_calls: snapshot.toolCalls,
				tools: snapshot.tools,
				last_tool: snapshot.lastTool ?? null,
				last_tool_args: snapshot.lastToolArgs ?? null,
				last_text: snapshot.lastText ?? null,
				tokens_in: snapshot.tokensIn,
				tokens_out: snapshot.tokensOut,
				cost: snapshot.cost,
			};
			try {
				writeJson(options.heartbeat, record);
			} catch {
				/* the heartbeat is best-effort; never fail the run for it */
			}
		};
		// The live view needs every event; only the heartbeat file is debounced.
		const emitProgress = () => {
			options.onProgress?.({
				...progress,
				tools: { ...progress.tools },
				updatedAt: nowSeconds(),
			});
		};
		const summariseArgs = (args: any): string | undefined => {
			if (args && typeof args === "object") {
				if (typeof args.command === "string") return args.command;
				if (typeof args.path === "string") return args.path;
				if (typeof args.file === "string") return args.file;
			}
			try {
				const text = JSON.stringify(args);
				return text && text !== "{}" ? text.slice(0, 120) : undefined;
			} catch {
				return undefined;
			}
		};

		const processLine = (line: string) => {
			if (!line.trim()) return;
			stream?.write(line + "\n");
			let event: any;
			try {
				event = JSON.parse(line);
			} catch {
				return;
			}
			switch (event.type) {
				case "turn_start":
					progress.turns += 1;
					break;
				case "tool_execution_start":
					progress.toolCalls += 1;
					progress.lastTool = String(event.toolName ?? "");
					progress.lastToolArgs = summariseArgs(event.args);
					progress.tools[progress.lastTool] =
						(progress.tools[progress.lastTool] ?? 0) + 1;
					break;
				case "tool_execution_end":
					if (event.toolName) progress.lastTool = String(event.toolName);
					break;
				case "message_update":
					if (event.usage) pendingUsage = event.usage;
					break;
				case "message_end":
					if (event.message?.role === "assistant") {
						for (const part of event.message.content ?? []) {
							if (part.type === "text") output = part.text;
						}
						const usage = event.message?.usage ?? pendingUsage;
						if (usage) {
							progress.tokensIn += usage.input ?? 0;
							progress.tokensOut += usage.output ?? 0;
							progress.cost += usage.cost?.total ?? 0;
						}
						pendingUsage = undefined;
						const text = output.trim();
						if (text) progress.lastText = text.slice(-280);
					}
					break;
				default:
					return;
			}
			flushHeartbeat();
			emitProgress();
		};

		proc.stdout.on("data", (data) => {
			buffer += data.toString();
			const lines = buffer.split("\n");
			buffer = lines.pop() ?? "";
			for (const line of lines) processLine(line);
		});
		proc.stderr.on("data", (data) => {
			stderr += data.toString();
			stream?.write(data.toString());
		});
		proc.on("close", (code, signal) => {
			if (buffer.trim()) processLine(buffer);
			stream?.end();
			flushHeartbeat(true);
			emitProgress();
			cleanup();
			resolve({
				exitCode: code ?? (signal ? 128 : 0),
				output,
				stderr,
				interrupted: signal != null,
				signal,
			});
		});
		proc.on("error", (err) => {
			stream?.end();
			flushHeartbeat(true);
			emitProgress();
			cleanup();
			resolve({ exitCode: 1, output, stderr: String(err) });
		});

		if (options.signal) {
			const kill = () => {
				try {
					proc.kill("SIGTERM");
				} catch {
					/* already gone */
				}
				// `proc.killed` turns true as soon as SIGTERM is sent, so it cannot
				// tell us whether the child actually exited.  Escalate on the real
				// exit state instead, or a worker that ignores SIGTERM hangs forever.
				const escalate = setTimeout(() => {
					if (proc.exitCode === null && proc.signalCode === null) {
						try {
							proc.kill("SIGKILL");
						} catch {
							/* already gone */
						}
					}
				}, 3000);
				escalate.unref();
			};
			if (options.signal.aborted) kill();
			else options.signal.addEventListener("abort", kill, { once: true });
		}
	});
}

// ---------------------------------------------------------------------------
// Live progress view (`docs/observability.md` §5, §7)
//
// `renderProgress` is pure: it reads one snapshot and returns terminal lines.
// The coordinator owns the registry and the render timer; this module owns the
// formatting, so a unit test can drive it with fixed timestamps and widths.
// ---------------------------------------------------------------------------

/** One running or finished subagent in the live view. */
export interface ProgressAgent {
	key: string;
	node: string;
	agent: string;
	unit?: string;
	attempt: number;
	status: "running" | "done" | "failed" | "interrupted";
	progress: SubagentProgress;
	finishedAt?: number;
}

/** One campaign node in the live view. */
export interface ProgressNode {
	id: string;
	status: string;
}

/** One wave in the live view. */
export interface ProgressWave {
	index: number;
	status: string;
	members: string[];
}

/** Everything `renderProgress` needs. The engine never enters this path. */
export interface ProgressSnapshot {
	campaign?: string;
	currentWave?: number;
	waves?: ProgressWave[];
	nodes?: ProgressNode[];
	agents: ProgressAgent[];
	now: number;
}

export interface RenderProgressOptions {
	/** Truncate every line to this many terminal columns. */
	width?: number;
	/** Style one line; `ctx.ui.theme.fg` fits. Default: no color. */
	color?: (name: string, text: string) => string;
	/** Maximum agent rows to show (default 8). */
	maxAgents?: number;
	/**
	 * Maximum total widget lines. Pi truncates a widget to the first ten lines
	 * (`MAX_WIDGET_LINES`), so the default ten keeps the whole view visible.
	 */
	maxLines?: number;
	/** Maximum wave rows in the window around the current wave (default 4). */
	maxWaves?: number;
	/** A running row is stale after this many seconds (default 5). */
	stalledAfterSeconds?: number;
	/** Override the clock for `renderAgentLine`. */
	now?: number;
}

const ANSI_PATTERN = /\x1b\[[0-9;]*m/g;

/** Terminal columns of one code point. Wide East-Asian and emoji count as two. */
function codePointWidth(code: number): number {
	if (code === 0) return 0;
	if (
		(code >= 0x1100 && code <= 0x115f) ||
		code === 0x2329 ||
		code === 0x232a ||
		(code >= 0x2e80 && code <= 0xa4cf && code !== 0x303f) ||
		(code >= 0xac00 && code <= 0xd7a3) ||
		(code >= 0xf900 && code <= 0xfaff) ||
		(code >= 0xfe30 && code <= 0xfe6f) ||
		(code >= 0xff00 && code <= 0xff60) ||
		(code >= 0xffe0 && code <= 0xffe6) ||
		(code >= 0x1f300 && code <= 0x1f64f) ||
		(code >= 0x1f900 && code <= 0x1f9ff) ||
		(code >= 0x20000 && code <= 0x3fffd)
	) {
		return 2;
	}
	return 1;
}

/** Visible terminal width of a string, ignoring ANSI escapes. */
export function visibleWidth(text: string): number {
	const plain = text.replace(ANSI_PATTERN, "");
	let width = 0;
	for (const char of plain) width += codePointWidth(char.codePointAt(0) ?? 0);
	return width;
}

/**
 * Truncate to a visible width and mark the cut with an ellipsis. Truncate the
 * plain text before you add color, so no escape sequence is cut in half.
 */
export function truncateToWidth(text: string, width: number): string {
	if (!Number.isFinite(width) || width <= 0) return "";
	if (visibleWidth(text) <= width) return text;
	let out = "";
	let used = 0;
	for (const char of text) {
		const size = codePointWidth(char.codePointAt(0) ?? 0);
		if (used + size > width - 1) break;
		out += char;
		used += size;
	}
	return `${out}…`;
}

/** Short human duration: `45s`, `3m12s`, `1h04m`. */
export function formatDuration(seconds: number): string {
	const total = Math.max(0, Math.floor(seconds));
	if (total < 60) return `${total}s`;
	const minutes = Math.floor(total / 60);
	if (minutes < 60) return `${minutes}m${String(total % 60).padStart(2, "0")}s`;
	return `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, "0")}m`;
}

/** One display marker per node or agent status. */
export function statusMarker(status: string): string {
	switch (status) {
		case "running":
			return "●";
		case "done":
			return "✓";
		case "failed":
			return "✗";
		case "interrupted":
		case "paused":
			return "⏸";
		case "recorded":
			return "◆";
		default:
			return "·";
	}
}

function fit(text: string, options: RenderProgressOptions): string {
	if (!options.width || options.width <= 0) return text;
	return truncateToWidth(text, options.width);
}

function lastToolText(progress: SubagentProgress): string | undefined {
	if (!progress.lastTool) return undefined;
	const args = progress.lastToolArgs ? ` ${progress.lastToolArgs}` : "";
	const text = `${progress.lastTool}${args}`;
	return text.length > 60 ? `${text.slice(0, 59)}…` : text;
}

/** One agent row: state, node, elapsed, counters, current tool or stall. */
export function renderAgentLine(
	agent: ProgressAgent,
	options: RenderProgressOptions = {},
): string {
	const color = options.color ?? ((_name: string, text: string) => text);
	const progress = agent.progress;
	const now = options.now ?? Date.now() / 1000;
	const end = agent.status === "running" ? now : (agent.finishedAt ?? progress.updatedAt);
	const bits = [formatDuration(Math.max(0, end - progress.startedAt))];
	if (progress.turns) bits.push(`${progress.turns} turns`);
	if (progress.toolCalls) bits.push(`${progress.toolCalls} tools`);
	let name = "muted";
	if (agent.status === "running") {
		const age = Math.max(0, now - progress.updatedAt);
		if (age > (options.stalledAfterSeconds ?? 5)) {
			bits.push(`⚠ stalled ${formatDuration(age)}`);
			name = "warning";
		} else {
			name = "accent";
			const tool = lastToolText(progress);
			if (tool) bits.push(tool);
		}
	} else if (agent.status === "done") {
		name = "success";
	} else if (agent.status === "failed") {
		name = "error";
	} else {
		name = "warning";
	}
	const text = `${statusMarker(agent.status)} ${agent.node} ${agent.agent}  ${bits.join(" · ")}`;
	return color(name, fit(text, options));
}

/**
 * A wave's display status. Derive it from the live node statuses, because the
 * cached `wave.status` in `state.json` is only refreshed on a replan and lags
 * a worker that just started.
 */
function waveStatus(wave: ProgressWave, nodes: ProgressNode[]): string {
	const byId = new Map(nodes.map((node) => [node.id, node.status]));
	const statuses = wave.members.map((id) => byId.get(id) ?? "pending");
	if (!statuses.length) return wave.status || "pending";
	if (statuses.every((status) => status === "done")) return "done";
	if (statuses.some((status) => status === "failed")) return "failed";
	const active = statuses.some(
		(status) =>
			status === "running" ||
			status === "recorded" ||
			status === "paused" ||
			status === "interrupted",
	);
	return active ? "running" : "pending";
}

/** The window of waves around the current one, so the busy wave stays visible. */
function waveWindow(
	waves: ProgressWave[],
	current: number,
	maxWaves: number,
): ProgressWave[] {
	if (maxWaves <= 0) return [];
	if (waves.length <= maxWaves) return waves;
	const position = waves.findIndex((wave) => wave.index === current);
	const currentPosition = position >= 0 ? position : 0;
	const start = Math.max(0, Math.min(currentPosition, waves.length - maxWaves));
	return waves.slice(start, start + maxWaves);
}

/**
 * Render the whole live view: a header, one aggregate line, one row per
 * subagent, then a window of the wave projection. Pure and allocation-light.
 *
 * The result never exceeds `maxLines` (default 10, pi's widget limit), so the
 * view is never truncated and the current wave is always one of the rows.
 */
export function renderProgress(
	snapshot: ProgressSnapshot,
	options: RenderProgressOptions = {},
): string[] {
	const color = options.color ?? ((_name: string, text: string) => text);
	const agents = snapshot.agents ?? [];
	const nodes = snapshot.nodes ?? [];
	const waves = snapshot.waves ?? [];
	if (!agents.length && !nodes.length && !waves.length) {
		return [color("dim", "sliceme: no plan")];
	}
	const now = snapshot.now;
	const maxLines = Math.max(3, options.maxLines ?? 10);
	const running = agents.filter((agent) => agent.status === "running");
	const finished = agents
		.filter((agent) => agent.status !== "running")
		.sort((a, b) => (b.finishedAt ?? 0) - (a.finishedAt ?? 0));
	const candidates = [...running, ...finished];

	const total = nodes.length || agents.length;
	const counted: Array<{ status: string }> = nodes.length ? nodes : agents;
	const done = counted.filter((item) => item.status === "done").length;
	const failed = counted.filter((item) => item.status === "failed").length;

	// Reserve wave rows first: late in a long campaign the current wave is the
	// most useful line, and pi would otherwise cut it off after the first ten.
	const statuses = waves.map((wave) => waveStatus(wave, nodes));
	const activeIndex = statuses.findIndex((status) => status !== "done");
	const current =
		snapshot.currentWave ?? (activeIndex >= 0 ? waves[activeIndex].index : waves.length);
	const waveCap = Math.max(0, Math.min(options.maxWaves ?? 4, maxLines - 2));
	const waveRows = waveWindow(waves, current, waveCap);
	const agentSlots = Math.max(
		0,
		Math.min(options.maxAgents ?? 8, maxLines - 2 - waveRows.length),
	);
	const shown = candidates.slice(0, agentSlots);

	const lines: string[] = [];
	const header = `sliceme${snapshot.campaign ? ` · ${snapshot.campaign}` : ""}`;
	lines.push(color("accent", fit(header, options)));

	const start = agents.reduce(
		(min, agent) => Math.min(min, agent.progress.startedAt || now),
		Number.POSITIVE_INFINITY,
	);
	const elapsed = Number.isFinite(start) ? now - start : 0;
	const summary: string[] = [];
	if (waves.length) {
		summary.push(`wave ${Math.min(current + 1, waves.length)}/${waves.length}`);
	}
	summary.push(`${done}/${total} done`);
	if (running.length) summary.push(`${running.length} running`);
	if (failed) summary.push(`${failed} failed`);
	if (elapsed >= 1) summary.push(formatDuration(elapsed));
	lines.push(color("muted", fit(summary.join(" · "), options)));

	const rowWidth = options.width && options.width > 2 ? options.width - 2 : options.width;
	const rows = shown.map(
		(agent) => `  ${renderAgentLine(agent, { ...options, width: rowWidth, now })}`,
	);
	const hiddenAgents = candidates.length - shown.length;
	// Replace a finished row with the marker; never hide a running worker, which
	// always sorts first.
	if (hiddenAgents > 0 && rows.length > running.length) {
		rows[rows.length - 1] = color("dim", fit(`  … ${hiddenAgents + 1} more`, options));
	}
	lines.push(...rows);

	const byId = new Map(nodes.map((node) => [node.id, node.status]));
	for (const wave of waveRows) {
		const members = wave.members
			.map((id) => `${statusMarker(byId.get(id) ?? "pending")} ${id}`)
			.join("  ");
		// Display waves one-based, the same as the aggregate line.
		lines.push(
			color("dim", fit(`─ wave ${wave.index + 1} [${waveStatus(wave, nodes)}]  ${members}`, options)),
		);
	}
	return lines;
}
