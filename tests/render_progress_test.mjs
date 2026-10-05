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
			tools: over.tools ?? {},
			toolSeconds: over.toolSeconds ?? 0,
			toolDurations: over.toolDurations ?? {},
			commands: over.commands ?? {},
			toolStartedAt: over.toolStartedAt,
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
const snapshotMetrics = lines.find((line) => line.includes("⏱"));
assert.ok(snapshotMetrics.includes("2m00s"), snapshotMetrics);
const row = lines.find((line) => line.includes("b1 worker"));
assert.ok(row, "the running agent has a row");
assert.ok(row.includes("2m00s"), row);
assert.ok(row.includes("7 turns"), row);
assert.ok(row.includes("23 tools"), row);
assert.ok(row.includes("bash cargo test"), row);
assert.ok(!row.includes("stalled"), row);
assert.ok(lines.some((line) => line.includes("─ wave 2 [running]")), lines.join("\n"));

// The wave status comes from the live node statuses, not the cached field that
// only refreshes on a replan: a wave left "pending" while its worker runs must
// display as running.
const staleWave = renderProgress({
	waves: [{ index: 0, status: "pending", members: ["a1"] }],
	nodes: [{ id: "a1", status: "running" }],
	agents: [agent({ node: "a1" })],
	now: 1120,
});
assert.ok(
	staleWave.some((line) => line.includes("─ wave 1 [running]")),
	staleWave.join("\n"),
);

// A silent running agent is labeled stalled, not silently shown as busy.
const stalled = renderProgress({
	agents: [agent({ now: 1120, updatedAt: 1000, startedAt: 1000 })],
	now: 1120,
});
const stalledRow = stalled.find((line) => line.includes("w1 worker"));
assert.ok(stalledRow.includes("⚠ stalled"), stalledRow);

// A finished agent keeps its final elapsed time and its time split.
assert.equal(
	renderAgentLine(
		agent({ status: "done", startedAt: 1000, finishedAt: 1060, toolSeconds: 20, now: 9999 }),
	),
	"✓ w1 worker  1m00s · 7 turns · 23 tools · tool 20s / thinking 40s",
);

// The metrics line shows the elapsed time, the tool split, and the slowest tool.
const measured = renderProgress({
	waves: [{ index: 0, status: "running", members: ["w1"] }],
	nodes: [{ id: "w1", status: "running" }],
	agents: [
		agent({
			node: "w1",
			startedAt: 1000,
			updatedAt: 1119,
			toolSeconds: 60,
			toolDurations: { bash: 45, read: 15 },
		}),
	],
	now: 1120,
});
const measuredMetrics = measured.find((line) => line.includes("⏱"));
assert.ok(measuredMetrics, measured.join("\n"));
assert.ok(measuredMetrics.includes("2m00s"), measuredMetrics);
assert.ok(measuredMetrics.includes("tools 50%"), measuredMetrics);
assert.ok(measuredMetrics.includes("thinking 50%"), measuredMetrics);
assert.ok(measuredMetrics.includes("slowest bash 45s"), measuredMetrics);

// The active tool call shows its age, so a long build is visible.
const inFlight = renderAgentLine(
	agent({
		node: "w1",
		lastTool: "bash",
		lastToolArgs: "make",
		toolStartedAt: 1060,
		updatedAt: 1119,
	}),
	{ now: 1120 },
);
assert.ok(inFlight.includes("bash make (1m00s)"), inFlight);

// The view stays within pi's ten-line widget limit and windows the waves, so
// the current wave is visible instead of the first four.
const manyWaves = [];
const manyNodes = [];
for (let index = 0; index < 13; index += 1) {
	const status = index < 5 ? "done" : "pending";
	manyWaves.push({ index, status, members: [`n${index}`] });
	manyNodes.push({ id: `n${index}`, status });
}
const manyAgents = Array.from({ length: 12 }, (_, index) =>
	agent({ node: `n${index}`, status: index < 1 ? "running" : "done", updatedAt: 1115 }),
);
const bounded = renderProgress({
	campaign: "big",
	waves: manyWaves,
	nodes: manyNodes,
	agents: manyAgents,
	now: 1120,
});
assert.ok(bounded.length <= 10, `too many widget lines: ${bounded.length}`);
assert.ok(bounded.some((line) => line.includes("─ wave 6 [pending]")), bounded.join("\n"));
assert.ok(!bounded.some((line) => line.includes("─ wave 1 ")), bounded.join("\n"));
assert.ok(bounded.some((line) => line.includes("more")), bounded.join("\n"));

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
