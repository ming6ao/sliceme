/**
 * Unit tests for the pure live-progress renderer in `common.ts`.
 *
 * `renderProgress` does no I/O, so this harness drives it with fixed timestamps
 * and widths. `tests/test_pi_package.py` runs it through Node's type stripping.
 */
import assert from "node:assert/strict";
import {
	formatDuration,
	renderAgentLine,
	renderProgress,
	truncateToWidth,
	visibleWidth,
} from "../integrations/pi/common.ts";

function agent(over = {}) {
	const startedAt = over.startedAt ?? 1000;
	return {
		key: `${over.agent ?? "worker"}:${over.node ?? "w1"}`,
		node: over.node ?? "w1",
		agent: over.agent ?? "worker",
		attempt: 1,
		status: over.status ?? "running",
		progress: {
			turns: over.turns ?? 7,
			toolCalls: over.toolCalls ?? 23,
			tools: {},
			tokensIn: 0,
			tokensOut: 0,
			cost: 0,
			lastTool: over.lastTool,
			lastToolArgs: over.lastToolArgs,
			startedAt,
			updatedAt: over.updatedAt ?? startedAt + 30,
		},
		finishedAt: over.finishedAt,
	};
}

// Durations.
assert.equal(formatDuration(0), "0s");
assert.equal(formatDuration(45), "45s");
assert.equal(formatDuration(192), "3m12s");
assert.equal(formatDuration(3840), "1h04m");

// Widths and truncation.
assert.equal(visibleWidth("abc"), 3);
assert.equal(visibleWidth("\x1b[32mabc\x1b[0m"), 3);
assert.equal(visibleWidth("日本語"), 6);
assert.equal(truncateToWidth("abcdef", 4), "abc…");
assert.equal(truncateToWidth("abc", 10), "abc");
assert.equal(truncateToWidth("abcdef", 0), "");

// Empty snapshot.
assert.deepEqual(renderProgress({ agents: [], now: 0 }), ["sliceme: no plan"]);

// Aggregate line, one row, and the wave projection.
const snapshot = {
	campaign: "demo",
	currentWave: 1,
	waves: [
		{ index: 0, status: "done", members: ["a1"] },
		{ index: 1, status: "running", members: ["b1"] },
	],
	nodes: [
		{ id: "a1", status: "done" },
		{ id: "b1", status: "running" },
	],
	agents: [agent({ node: "b1", lastTool: "bash", lastToolArgs: "cargo test", updatedAt: 1115 })],
	now: 1120,
};
const lines = renderProgress(snapshot);
assert.equal(lines[0], "sliceme · demo");
assert.ok(lines[1].includes("wave 2/2"), lines[1]);
assert.ok(lines[1].includes("1/2 done"), lines[1]);
assert.ok(lines[1].includes("1 running"), lines[1]);
assert.ok(lines[1].includes("2m00s"), lines[1]);
const row = lines.find((line) => line.includes("b1 worker"));
assert.ok(row, "the running agent has a row");
assert.ok(row.includes("2m00s"), row);
assert.ok(row.includes("7 turns"), row);
assert.ok(row.includes("23 tools"), row);
assert.ok(row.includes("bash cargo test"), row);
assert.ok(!row.includes("stalled"), row);
assert.ok(lines.some((line) => line.includes("─ wave 1 [running]")), lines.join("\n"));

// A silent running agent is labeled stalled, not silently shown as busy.
const stalled = renderProgress({
	agents: [agent({ now: 1120, updatedAt: 1000, startedAt: 1000 })],
	now: 1120,
});
const stalledRow = stalled.find((line) => line.includes("w1 worker"));
assert.ok(stalledRow.includes("⚠ stalled"), stalledRow);

// A finished agent keeps its final elapsed time.
assert.equal(
	renderAgentLine(agent({ status: "done", startedAt: 1000, finishedAt: 1060, now: 9999 })),
	"✓ w1 worker  1m00s · 7 turns · 23 tools",
);

// Every line fits the requested width.
for (const width of [12, 24, 40]) {
	const rendered = renderProgress(snapshot, { width });
	for (const line of rendered) {
		assert.ok(
			visibleWidth(line) <= width,
			`line wider than ${width}: ${JSON.stringify(line)} (${visibleWidth(line)})`,
		);
	}
}

// The color hook receives a theme color name and the final text.
const colored = renderAgentLine(agent({ status: "failed" }), {
	color: (name, text) => `<${name}>${text}`,
});
assert.ok(colored.startsWith("<error>"), colored);

console.log("render_progress_test: ok");
