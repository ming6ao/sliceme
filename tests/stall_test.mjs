/**
 * Unit tests for the coordinator budget tick's stall wave in
 * `integrations/pi/stall-wave.ts`.
 *
 * The wave is plain Node I/O over fake campaign state, so this harness pins
 * the one-notify-per-frozen-`last_ts` mark logic, the `waited_ms`
 * accumulation, the descriptor write, and the liveness filter that keeps a
 * recorded node's frozen mirror from becoming a phantom stall — without a pi
 * runtime. `tests/test_pi_package.py` runs it through Node's type stripping.
 */
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { STALL_AFTER_MS, measureStallWave } from "../integrations/pi/stall-wave.ts";

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "sliceme-stall-"));
process.on("exit", () => fs.rmSync(tmp, { recursive: true, force: true }));

const progressDir = path.join(tmp, "feat--x.progress");
const descriptorFile = path.join(tmp, "feat--x.session.json");
fs.mkdirSync(progressDir);

assert.equal(STALL_AFTER_MS, 120_000, "the wave matches the engine's 120 s stall threshold");

function writeMirror(node, ts, extra = {}) {
	fs.writeFileSync(
		path.join(progressDir, `${node}.json`),
		JSON.stringify({ node, step: 3, name: "edit x.py", tool: "edit", ts, ...extra }),
	);
}

function writeDescriptor(descriptor) {
	fs.writeFileSync(descriptorFile, JSON.stringify(descriptor, null, 2) + "\n");
}

function readDescriptor() {
	return JSON.parse(fs.readFileSync(descriptorFile, "utf8"));
}

// The tick reuses one marks map, so the harness keeps it across ticks.
const marks = new Map();
const messages = [];

function runWave(nowMs) {
	messages.length = 0;
	measureStallWave({
		progressDir,
		descriptorFile,
		nowMs,
		marks,
		notify: (message) => messages.push(message),
	});
	return messages;
}

const CLOCK = 1000_000;
const FROZEN_TS = (CLOCK - STALL_AFTER_MS) / 1000 - 1; // silenced 1 s past the threshold

// --- One mark per frozen last_ts ----------------------------------------------

// A running node silent past the threshold is marked once, with the stall
// count and the accumulated waiting time in the descriptor.
writeMirror("n0", FROZEN_TS);
writeDescriptor({ status: "suspended", nodes: { n0: { status: "running" } } });
{
	runWave(CLOCK);
	assert.equal(marks.get("n0"), FROZEN_TS, "the mark holds the frozen last_ts");
	assert.equal(messages.length, 1, "one notify for one fresh mark");
	assert.ok(messages[0].includes("node n0 is stalled"), messages[0]);
	const entry = readDescriptor().nodes.n0;
	assert.equal(entry.status, "running", "the mark preserves the node's own status");
	assert.equal(entry.stalls, 1);
	assert.ok(entry.stalled_ms >= STALL_AFTER_MS, "stalled_ms is the silence age");
	assert.equal(entry.waited_ms, entry.stalled_ms, "the first mark accumulates once");
}

// The same frozen last_ts never marks nor notifies twice.
{
	runWave(CLOCK + 15_000);
	assert.equal(messages.length, 0, "one frozen last_ts notifies once");
	assert.equal(marks.size, 1, "no fresh mark on the same frozen last_ts");
	assert.equal(marks.get("n0"), FROZEN_TS, "the mark still holds the frozen last_ts");
	const entry = readDescriptor().nodes.n0;
	assert.equal(entry.stalls, 1, "the descriptor keeps the first mark");
}

// After a new event moves the mirror, the next tick marks again and the
// waiting time accumulates.
{
	writeMirror("n0", (CLOCK + 15_000) / 1000);
	runWave(CLOCK + 300_000);
	assert.equal(messages.length, 1, "a new event re-arms the notify");
	const entry = readDescriptor().nodes.n0;
	assert.equal(entry.stalls, 2, "two silences, two marks");
	assert.ok(entry.waited_ms > entry.stalled_ms, "waited_ms accumulated both marks");
}

// --- The liveness filter -------------------------------------------------

// A fresh tick process starts with an empty marks bookkeeping.
marks.clear();
// A node whose descriptor status is `recorded` is finished work: its mirror
// froze at `record` and grows stale forever, so the wave never marks or
// notifies it, and never writes the descriptor for it.
fs.unlinkSync(path.join(progressDir, "n0.json"));
writeMirror("n9", FROZEN_TS);
writeMirror("n8", FROZEN_TS);
writeDescriptor({
	status: "suspended",
	nodes: { n9: "recorded", n8: { status: "recorded", stalls: 1, waited_ms: 5000 } },
});
{
	runWave(CLOCK);
	assert.equal(marks.size, 0, "no mark for a recorded node");
	assert.equal(messages.length, 0, "no notify for a recorded node");
	const descriptor = readDescriptor();
	const before = fs.readFileSync(descriptorFile, "utf8");
	assert.equal(descriptor.nodes.n9, "recorded", "a plain recorded entry stays untouched");
	assert.deepEqual(
		{ ...descriptor.nodes.n8 },
		{ status: "recorded", stalls: 1, waited_ms: 5000 },
		"the recorded object entry keeps its counters",
	);
	assert.equal(fs.readFileSync(descriptorFile, "utf8"), before, "the wave writes nothing here");
}

// A still-running sibling beside the recorded ones is untouched by the filter.
{
	writeMirror("n7", FROZEN_TS);
	writeDescriptor({
		status: "suspended",
		nodes: { n7: { status: "running" }, n9: "recorded" },
	});
	runWave(CLOCK);
	assert.equal(marks.get("n7"), FROZEN_TS, "the running sibling is still marked");
	assert.equal(readDescriptor().nodes.n7.stalls, 1);
}

// --- The quiet paths ------------------------------------------------------

marks.clear();
// A silent node below the threshold stays unmarked.
fs.unlinkSync(path.join(progressDir, "n7.json"));
fs.unlinkSync(path.join(progressDir, "n8.json"));
fs.unlinkSync(path.join(progressDir, "n9.json"));
{
	writeMirror("n1", (CLOCK - STALL_AFTER_MS) / 1000 + 5); // 5 s under the threshold
	writeDescriptor({ status: "suspended", nodes: {} });
	runWave(CLOCK);
	assert.equal(marks.size, 0);
	assert.equal(messages.length, 0);
	assert.deepEqual(readDescriptor().nodes, {}, "the descriptor keeps no entry");
}

// A mirror with no usable `ts` and a missing channel are both ignored.
{
	fs.writeFileSync(path.join(progressDir, "n2.json"), JSON.stringify({ node: "n2" }));
	assert.doesNotThrow(() => runWave(CLOCK));
	fs.rmSync(progressDir, { recursive: true });
	assert.doesNotThrow(() => runWave(CLOCK));
}

console.log("stall wave harness: ok");
