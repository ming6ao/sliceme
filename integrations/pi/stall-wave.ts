/**
 * The stall wave of the coordinator's budget tick.
 *
 * The tick reads the progress mirrors (`.sliceme/<key>.progress/<node>.json`),
 * marks a node that has been silent past the engine's stall threshold in the
 * adapter-owned session descriptor, and notifies the session. The wave is
 * read-only where the campaign is concerned: it never aborts a turn, and the
 * child still ends at its own deadline (docs/progress-watchdog.md §4.3).
 *
 * One frozen `last_ts` notifies once: the next mark needs a new event first.
 * A node whose descriptor entry already carries a terminal status (`recorded`)
 * is skipped entirely: the writer never deletes mirrors, so a node recorded at
 * a phase boundary keeps a frozen mirror whose age grows forever, and a stall
 * mark for finished work would be a phantom (report, `status --dense`, and
 * `resume` all read the descriptor's stall counts).
 *
 * The module is import-free apart from `node:` builtins, so a plain-Node
 * harness can drive it like `campaign-resource.ts`.
 */

import * as fs from "node:fs";
import * as path from "node:path";

/**
 * The stall threshold the coordinator ticks with, in milliseconds.
 * It is the millisecond form of `sliceme/progress.py`'s
 * `STALL_AFTER_SECONDS`, so the engine's `progress` attention flag and this
 * notification agree.
 */
export const STALL_AFTER_MS = 120_000;

/** Whether a node status counts as finished for the stall wave. */
export function isFinishedStatus(status: string): boolean {
	return status === "recorded";
}

function readJson<T>(file: string, fallback: T): T {
	try {
		return JSON.parse(fs.readFileSync(file, "utf8")) as T;
	} catch {
		return fallback;
	}
}

/** Write a JSON file atomically: a sibling temporary file, then a rename. */
function writeJson(file: string, data: unknown): void {
	fs.mkdirSync(path.dirname(file), { recursive: true });
	const tmp = `${file}.${process.pid}.${Date.now()}.tmp`;
	fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + "\n", "utf8");
	fs.renameSync(tmp, file);
}

export interface StallWaveParams {
	/** The campaign's progress channel (`.sliceme/<key>.progress`). */
	progressDir: string;
	/** The campaign's session descriptor (`.sliceme/<key>.session.json`). */
	descriptorFile: string;
	/** The tick's clock, in milliseconds. */
	nowMs: number;
	/** The per-node frozen `last_ts` marks; the wave owns one, reused across ticks. */
	marks: Map<string, number>;
	/** The session notification, or undefined when the session has no UI. */
	notify?: (message: string) => void;
}

/**
 * Read the progress mirrors, mark stalled children in the descriptor, and
 * notify the session. Delegates the tick's whole stall work; `marks` carries
 * the one-notify-per-frozen-`last_ts` bookkeeping between ticks.
 */
export function measureStallWave(params: StallWaveParams): void {
	const { progressDir, descriptorFile, nowMs, marks, notify } = params;
	let names: string[];
	try {
		names = fs.readdirSync(progressDir);
	} catch {
		return; // no channel yet: nothing has run
	}
	const descriptor = readJson<any>(descriptorFile, undefined);
	const nowSeconds = nowMs / 1000;
	let marked = false;
	for (const name of names.sort()) {
		if (!name.endsWith(".json") || name.endsWith(".jsonl")) continue;
		const mirror = readJson<any>(path.join(progressDir, name), null);
		const node =
			mirror && typeof mirror.node === "string"
				? mirror.node
				: name.slice(0, -".json".length);
		const lastTs = Number(mirror?.ts);
		if (!Number.isFinite(lastTs) || lastTs <= 0) continue;
		const stalledMs = Math.max(0, Math.round((nowSeconds - lastTs) * 1000));
		if (stalledMs < STALL_AFTER_MS) continue;
		if (marks.get(node) === lastTs) continue;
		if (descriptor && typeof descriptor === "object") {
			const nodes =
				descriptor.nodes && typeof descriptor.nodes === "object"
					? descriptor.nodes
					: {};
			const existing = nodes[node];
			const prior = existing && typeof existing === "object" ? existing : {};
			// A recorded node's mirror froze at `record` and never moves again:
			// its age grows forever, but it is finished work, not a stall.
			const status =
				prior.status || (typeof existing === "string" && existing) || "";
			if (isFinishedStatus(status)) continue;
			nodes[node] = {
				...prior,
				status: prior.status || (typeof existing === "string" && existing) || "running",
				stalls: Number(prior.stalls || 0) + 1,
				stalled_ms: stalledMs,
				waited_ms: Number(prior.waited_ms || 0) + stalledMs,
			};
			descriptor.nodes = nodes;
			marked = true;
		}
		// Only a fresh mark notifies: one notify per frozen `last_ts`.
		marks.set(node, lastTs);
		if (notify) {
			notify(
				`sliceme: node ${node} is stalled (no progress for ${Math.round(stalledMs / 1000)}s); ` +
					"the child still ends at its own deadline",
			);
		}
	}
	if (marked) {
		try {
			writeJson(descriptorFile, descriptor);
		} catch {
			/* a mark must never break the tick */
		}
	}
}
