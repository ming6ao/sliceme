/**
 * The per-node child progress writer for the Sliceme pi package extension.
 *
 * Background campaign children load the installed package, so this module's
 * hook runs inside every worker. It maps one child tool result to one
 * progress event, throttled to at most one line per second per child, and
 * appends it to the engine's read-only channel:
 *
 *   `.sliceme/<branch-key>.progress/<node>.jsonl`  (the append-only tail)
 *   `.sliceme/<branch-key>.progress/<node>.json`   (the last event, mirrored)
 *
 * The launch passes the mapping through `options.extensionBindings`
 * (`{"sliceme/1": {"campaign": key, "node": id}}`), so this module reads
 * `PI_SUBAGENT_EXTENSION_BINDINGS` at extension load (the factory runs once
 * per child session). The model does nothing and spends no tokens on
 * progress. The fallback when the ambient extension has no bindings is the
 * `sliceme_progress` write-only tool: the worker calls it once per completed
 * task and the writer records the same event shape.
 *
 * The engine owns the reads (`sliceme/progress.py`); this module owns the
 * writes, including the tail rotation at `PROGRESS_TAIL_BYTES`. The files
 * are plane state: never committed.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import {
	branchKey,
	stateDir,
} from "./common.ts";

/** The tail rotation cap, in bytes (mirrors `sliceme/progress.py`). */
export const PROGRESS_TAIL_BYTES = 65536;

/** How much of the tail one rotation keeps (mirrors `sliceme/progress.py`). */
export const PROGRESS_TAIL_KEEP_LINES = 128;

/** The writer throttle: at most one event per second per child. */
export const PROGRESS_WRITE_INTERVAL_MS = 1000;

/** The extension-bindings namespace this version of the writer reads. */
export const SLICEME_BINDING_NAMESPACE = "sliceme/1";

const CAMPAIGN_TOKEN = /^[A-Za-z0-9._-]{1,128}$/;

export interface ProgressBinding {
	/** The campaign's branch key (the file prefix, `feat--x`). */
	campaign: string;
	/** The DAG node id the child implements. */
	node: string;
	/** The workflow child key (`node-<id>`, a retake `node-<id>-try-2`). */
	run: string;
	/** The absolute `.sliceme` directory the files live under. */
	dir: string;
}

/**
 * Parse the launcher's `PI_SUBAGENT_EXTENSION_BINDINGS` payload and return
 * this package's binding. It returns `undefined` when the payload is absent
 * or does not carry both a campaign and a node.
 */
export function parseProgressBindings(
	raw: string | undefined | null,
): ProgressBinding | undefined {
	if (!raw || typeof raw !== "string") return undefined;
	let parsed: any;
	try {
		parsed = JSON.parse(raw);
	} catch {
		return undefined;
	}
	if (!parsed || typeof parsed !== "object") return undefined;
	const ours = parsed[SLICEME_BINDING_NAMESPACE];
	if (!ours || typeof ours !== "object") return undefined;
	const campaign = typeof ours.campaign === "string" ? ours.campaign.trim() : "";
	const node = typeof ours.node === "string" ? ours.node.trim() : "";
	if (!campaign || !node) return undefined;
	// The binding names the branch key directly; be strict about the token
	// because it becomes one file name under .sliceme/.
	if (campaign.includes("/") || !CAMPAIGN_TOKEN.test(campaign)) return undefined;
	if (node.includes("/") || node.includes("\\") || node.includes("..")) return undefined;
	const run =
		typeof ours.run === "string" && ours.run.trim() ? ours.run.trim() : node;
	const dir = typeof ours.dir === "string" && ours.dir.trim()
		? path.resolve(ours.dir.trim())
		: stateDir(process.cwd());
	return { campaign, node, run, dir };
}

/** The node's tail path under the campaign's progress directory. */
export function progressTailPath(info: ProgressBinding): string {
	return path.join(
		info.dir,
		`${branchKey(info.campaign)}.progress`,
		`${info.node}.jsonl`,
	);
}

/** The node's mirror path (the last event as one JSON object). */
export function progressMirrorPath(info: ProgressBinding): string {
	return path.join(
		info.dir,
		`${branchKey(info.campaign)}.progress`,
		`${info.node}.json`,
	);
}

/** One progress event, as one JSON line of the tail. */
export interface ProgressEvent {
	node: string;
	step: number;
	name: string;
	tool: string;
	ts: number;
	detail: string;
	run_id: string;
}

/**
 * Build one event from a child tool result. `name` is a short report of the
 * call (the first bounded string argument), `detail` marks an error result.
 * The caller supplies the step counter and the throttle clock.
 */
export function buildProgressEvent(
	info: ProgressBinding,
	toolName: string,
	input: Record<string, unknown>,
	isError: boolean,
	step: number,
	nowSeconds: number,
	nameHint?: string,
): ProgressEvent {
	let name = nameHint ?? "";
	if (!name) {
		for (const value of Object.values(input ?? {})) {
			if (typeof value === "string" && value.trim()) {
				name = value.trim().replace(/\s+/g, " ").slice(0, 96);
				break;
			}
		}
	}
	return {
		node: info.node,
		step,
		name,
		tool: toolName || "",
		ts: nowSeconds,
		detail: isError ? "error" : "",
		run_id: info.run,
	};
}

/** Serialize one event as its tail line (one compact JSON line). */
export function tailLine(event: ProgressEvent): string {
	return JSON.stringify(event);
}

/**
 * Rotate the tail so it stays bounded: when it is over the byte cap, keep
 * only the last `PROGRESS_TAIL_KEEP_LINES` lines. The rotation is part of
 * the write: no caller ever reads an unbounded tail.
 */
export function rotateTail(tailPath: string): void {
	try {
		const stat = fs.statSync(tailPath);
		if (stat.size <= PROGRESS_TAIL_BYTES) return;
		const raw = fs.readFileSync(tailPath, "utf8");
		const lines = raw.split("\n").filter((line) => line.trim());
		const kept = lines.slice(-PROGRESS_TAIL_KEEP_LINES);
		const next = kept.join("\n") + "\n";
		fs.writeFileSync(tailPath, next, "utf8");
	} catch {
		/* a missing tail needs no rotation */
	}
}

/**
 * Append one event line and update the mirror. Both writes are bounded and
 * appended in the child's one event loop; no lock is needed.
 */
export function writeProgress(info: ProgressBinding, event: ProgressEvent): void {
	const tail = progressTailPath(info);
	const mirror = progressMirrorPath(info);
	fs.mkdirSync(path.dirname(tail), { recursive: true });
	rotateTail(tail);
	fs.appendFileSync(tail, tailLine(event) + "\n", "utf8");
	fs.writeFileSync(mirror, JSON.stringify(event, null, 2) + "\n", "utf8");
}

/**
 * The per-child writer state: the step counter and the throttle clock.
 * Both live for the child session's lifetime; two children share nothing
 * because each child session loads its own extension instance.
 */
export class ProgressWriter {
	private readonly info: ProgressBinding;
	private step = 0;
	private lastWriteMs = 0;

	constructor(info: ProgressBinding) {
		this.info = info;
	}

	get binding(): ProgressBinding {
		return this.info;
	}

	/**
	 * Record one tool result. Returns the written step, or `null` when the
	 * throttle swallowed the event (at most one line per second per child).
	 */
	observe(
		toolName: string,
		input: Record<string, unknown>,
		isError: boolean,
		nowMs: number = Date.now(),
		nameHint?: string,
	): number | null {
		if (nowMs - this.lastWriteMs < PROGRESS_WRITE_INTERVAL_MS) return null;
		this.lastWriteMs = nowMs;
		this.step += 1;
		const event = buildProgressEvent(
			this.info,
			toolName,
			input,
			isError,
			this.step,
			nowMs / 1000,
			nameHint,
		);
		try {
			writeProgress(this.info, event);
		} catch {
			return null;
		}
		return this.step;
	}

	/**
	 * The fallback path: the worker's own `sliceme_progress` tool call.
	 * The same throttle applies, so the channel stays at one line per
	 * second per child whichever writer fired last.
	 */
	record(
		nameHint: string,
		detail: string = "",
		nowMs: number = Date.now(),
	): number | null {
		return this.observe(
			"sliceme_progress",
			{ name: nameHint, detail },
			false,
			nowMs,
			nameHint,
		);
	}
}
