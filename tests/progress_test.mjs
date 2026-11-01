/**
 * Unit tests for the child progress writer in
 * `integrations/pi/progress.ts`.
 *
 * The writer is plain Node I/O over a fake campaign directory, so this
 * harness pins the binding parsing, the one-line-per-second throttle, the
 * mirror update, and the tail rotation without a pi runtime.
 * `tests/test_pi_package.py` runs it through Node's type stripping.
 */
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
	PROGRESS_TAIL_BYTES,
	PROGRESS_TAIL_KEEP_LINES,
	ProgressWriter,
	buildProgressEvent,
	parseProgressBindings,
	progressMirrorPath,
	progressTailPath,
} from "../integrations/pi/progress.ts";

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "sliceme-progress-"));
process.on("exit", () => fs.rmSync(tmp, { recursive: true, force: true }));

const SLICEME_DIR = tmp;
const BINDINGS = JSON.stringify({
	"sliceme/1": {
		campaign: "feat--x",
		node: "n7",
		run: "node-n7",
		dir: SLICEME_DIR,
	},
});

// --- The bindings ------------------------------------------------------------

const parsed = parseProgressBindings(BINDINGS);
assert.ok(parsed, "both a campaign and a node bind the writer");
assert.equal(parsed.campaign, "feat--x");
assert.equal(parsed.node, "n7");
assert.equal(parsed.run, "node-n7");
assert.equal(parsed.dir, SLICEME_DIR);

// The file paths follow the engine's channel layout.
assert.equal(progressTailPath(parsed), path.join(SLICEME_DIR, "feat--x.progress", "n7.jsonl"));
assert.equal(progressMirrorPath(parsed), path.join(SLICEME_DIR, "feat--x.progress", "n7.json"));

// A session without bindings (the coordinator's own session) writes nothing.
assert.equal(parseProgressBindings(undefined), undefined);
assert.equal(parseProgressBindings(""), undefined);
assert.equal(parseProgressBindings("not json"), undefined);
assert.equal(parseProgressBindings("{}"), undefined);
assert.equal(parseProgressBindings('{"sliceme/1": {}}'), undefined);
const noRun = parseProgressBindings('{"sliceme/1": {"campaign": "feat--x", "node": "n7"}}');
assert.ok(noRun, "a binding without a run key still binds");
assert.equal(noRun.run, "n7", "the run key falls back to the node id");

// The campaign token is the branch key, so it stays strict.
assert.equal(
	parseProgressBindings('{"sliceme/1": {"campaign": "feat/x", "node": "n7"}}'),
	undefined,
);
assert.equal(
	parseProgressBindings('{"sliceme/1": {"campaign": "feat--x", "node": "../escape"}}'),
	undefined,
);

// --- One event ----------------------------------------------------------------

const event = buildProgressEvent(
	parsed,
	"edit",
	{ file_path: "src/api/payments.py" },
	false,
	4,
	1731234567.5,
);
assert.deepEqual(
	{ node: event.node, step: event.step, name: event.name, tool: event.tool, ts: event.ts, detail: event.detail, run_id: event.run_id },
	{ node: "n7", step: 4, name: "src/api/payments.py", tool: "edit", ts: 1731234567.5, detail: "", run_id: "node-n7" },
);
const line = JSON.stringify(event);
assert.equal(line.split("\n").length, 1, "one event is one JSON line");

// An error result carries a visible detail.
const failed = buildProgressEvent(parsed, "bash", { command: "false" }, true, 5, 1, "false");
assert.equal(failed.detail, "error");

// --- The writer: throttle, mirror, tail ---------------------------------------

const writer = new ProgressWriter(parsed);

// Two results inside one second collapse to one line: at most one line per
// second per child.
assert.equal(writer.observe("edit", { file_path: "a.py" }, false, 1000), 1);
assert.equal(writer.observe("edit", { file_path: "b.py" }, false, 1500), null);
assert.equal(writer.observe("read", { file_path: "c.py" }, false, 1999), null);
assert.equal(writer.observe("bash", { command: "make" }, false, 2000), 2);

const tail = fs.readFileSync(progressTailPath(parsed), "utf8").trim().split("\n");
assert.equal(tail.length, 2, "the throttle kept two lines total");
const second = JSON.parse(tail[1]);
assert.deepEqual(
	{ step: second.step, name: second.name, tool: second.tool, run_id: second.run_id },
	{ step: 2, name: "make", tool: "bash", run_id: "node-n7" },
);

// The mirror holds the last event as one JSON object.
const mirror = JSON.parse(fs.readFileSync(progressMirrorPath(parsed), "utf8"));
assert.equal(mirror.step, 2);
assert.equal(mirror.name, "make");
assert.equal(mirror.node, "n7");
assert.equal(mirror.ts > 0, true);

// The tool fallback writes the same shape and shares the throttle.
assert.equal(writer.record("finished task one", "", 12000), 3);
const toolLine = JSON.parse(
	fs.readFileSync(progressTailPath(parsed), "utf8").trim().split("\n").pop(),
);
assert.equal(toolLine.step, 3);
assert.equal(toolLine.tool, "sliceme_progress");
assert.equal(toolLine.name, "finished task one");
assert.equal(toolLine.run_id, "node-n7");

// A child that failed a tool write keeps going: the next second works.
assert.equal(writer.observe("edit", { file_path: "a.py" }, false, 45_000), 4);

// --- The rotation -------------------------------------------------------------

const rotateBindings = parseProgressBindings(
	JSON.stringify({
		"sliceme/1": { campaign: "feat--y", node: "n1", run: "node-n1-try-2", dir: SLICEME_DIR },
	}),
);
assert.equal(rotateBindings.run, "node-n1-try-2", "the retake run key rides the tail");
const rotator = new ProgressWriter(rotateBindings);
// One event is roughly 95 bytes, so 800 events pass the 64 KiB cap.
for (let index = 1; index <= 800; index++) {
	const written = rotator.observe("edit", { file_path: "a.py" }, false, index * 2000);
	assert.ok(written, `event ${index} wrote`);
}
const rotated = fs.readFileSync(progressTailPath(rotateBindings), "utf8").trim().split("\n");
assert.ok(rotated.length > 0, "the rotated tail keeps events");
assert.ok(
	rotated.length <= 800,
	`the tail rotated (it kept ${rotated.length} of 800 events)`,
);
const last = JSON.parse(rotated.at(-1));
assert.equal(last.run_id, "node-n1-try-2", "the tail keeps the newest events");
assert.equal(JSON.parse(rotated[0]).step > PROGRESS_TAIL_KEEP_LINES - 1, true, "the rotation dropped the oldest events, not the newest");
const size = fs.statSync(progressTailPath(rotateBindings)).size;
assert.ok(size <= PROGRESS_TAIL_BYTES + 512, `the rotated tail is ${size} bytes`);
// The mirror reflects the newest event.
const rotateMirror = JSON.parse(fs.readFileSync(progressMirrorPath(rotateBindings), "utf8"));
assert.equal(rotateMirror.step, 800);
assert.equal(rotateMirror.name, "a.py");
