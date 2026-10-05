/**
 * Unit tests for the time and tool metrics helpers in `common.ts`.
 *
 * The reducer cases are pure functions, so this harness drives them with a
 * fixed event list. `tests/test_pi_package.py` runs it through Node's type
 * stripping.
 */
import assert from "node:assert/strict";
import {
	programName,
	reduceToolEnd,
	reduceToolStart,
	summariseArgs,
	topCommands,
} from "../integrations/pi/common.ts";

function blank() {
	return {
		turns: 0,
		toolCalls: 0,
		tools: {},
		toolSeconds: 0,
		toolDurations: {},
		commands: {},
		tokensIn: 0,
		tokensOut: 0,
		cost: 0,
		startedAt: 0,
		updatedAt: 0,
	};
}

// Command grouping uses the program name only.
assert.equal(programName("tools/nanochat build //src:tokenizer"), "tools/nanochat");
assert.equal(programName("cd /repo && python3 - <<'PY'"), "python3");
assert.equal(programName("  FOO=1 BAR=2 cargo test  "), "cargo");
assert.equal(programName("git\ncommit"), "git");
assert.equal(programName(""), undefined);
assert.equal(programName(undefined), undefined);

// The argument summary prefers the command, then the path.
assert.equal(summariseArgs({ command: "make test" }), "make test");
assert.equal(summariseArgs({ path: "src/x.cc" }), "src/x.cc");

// The command rollup sorts by total seconds and honors the cap.
const commands = {
	python3: { tool: "bash", seconds: 10, calls: 2 },
	cargo: { tool: "bash", seconds: 30, calls: 1 },
};
const ranked = topCommands(commands, 1);
assert.equal(ranked.length, 1);
assert.equal(ranked[0].command, "cargo");
assert.equal(ranked[0].seconds, 30);
assert.equal(topCommands(commands, 10).length, 2);

// One start and one end pair by toolCallId, and the command aggregates.
const progress = blank();
const active = new Map();
reduceToolStart(
	progress,
	active,
	{ toolName: "bash", toolCallId: "a", args: { command: "cargo test" } },
	100,
);
assert.equal(active.size, 1);
reduceToolEnd(progress, active, { toolName: "bash", toolCallId: "a" }, 130);
assert.equal(active.size, 0);
assert.equal(progress.toolCalls, 1);
assert.equal(progress.toolSeconds, 30);
assert.equal(progress.toolDurations.bash, 30);
assert.equal(progress.commands.cargo.seconds, 30);
assert.equal(progress.commands.cargo.calls, 1);
assert.equal(progress.toolStartedAt, undefined);

// Parallel calls interleave, and the ids keep them apart.
const parallel = blank();
const running = new Map();
reduceToolStart(parallel, running, { toolName: "read", toolCallId: "r", args: { path: "a" } }, 200);
reduceToolStart(
	parallel,
	running,
	{ toolName: "bash", toolCallId: "b", args: { command: "sleep 5" } },
	200,
);
assert.equal(parallel.toolStartedAt, 200);
reduceToolEnd(parallel, running, { toolName: "bash", toolCallId: "b" }, 205);
reduceToolEnd(parallel, running, { toolName: "read", toolCallId: "r" }, 210);
assert.equal(parallel.toolDurations.bash, 5);
assert.equal(parallel.toolDurations.read, 10);
assert.equal(parallel.toolSeconds, 15);
assert.equal(parallel.toolStartedAt, undefined);

// An end without a matching start does not throw and records no duration.
const orphan = blank();
reduceToolEnd(orphan, new Map(), { toolName: "bash", toolCallId: "missing" }, 300);
assert.equal(orphan.toolSeconds, 0);
assert.equal(orphan.lastTool, "bash");

console.log("metrics_test: ok");
