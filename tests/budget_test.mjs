/**
 * Unit tests for the campaign budget module in `integrations/pi/budget.ts`.
 *
 * The module is pure, so this harness pins the cap reading, the active-second
 * accumulator, the session cost sum, and the trip test without a pi runtime.
 * `tests/test_pi_package.py` runs it through Node's type stripping.
 */
import assert from "node:assert/strict";
import {
	budgetWindowChanged,
	evaluateBudget,
	hasCampaignBudget,
	nextActiveSeconds,
	readBudgetCaps,
	sessionCostUsd,
	spentUsd,
} from "../integrations/pi/budget.ts";

// readBudgetCaps: valid values survive, invalid values drop out.
assert.deepEqual(readBudgetCaps({ budget: { wall_seconds: 60, cost_usd: 2.5 } }), {
	wallSeconds: 60,
	costUsd: 2.5,
});
assert.deepEqual(readBudgetCaps({}), {});
assert.deepEqual(readBudgetCaps(null), {});
assert.deepEqual(readBudgetCaps({ budget: { wall_seconds: 0, cost_usd: -1 } }), {});
assert.deepEqual(readBudgetCaps({ budget: { wall_seconds: "60", cost_usd: "2.5" } }), {
	wallSeconds: 60,
	costUsd: 2.5,
});
assert.deepEqual(readBudgetCaps({ budget: "60" }), {});
assert.ok(hasCampaignBudget({ wallSeconds: 1 }));
assert.ok(hasCampaignBudget({ costUsd: 1 }));
assert.ok(!hasCampaignBudget({}));

// nextActiveSeconds: the first tick starts at the declared window, later ticks
// start at the last tick, and the clamp keeps a crash gap out.
assert.equal(nextActiveSeconds(undefined, 1000, 30, 990), 10);
assert.equal(nextActiveSeconds(undefined, 1000, 30), 0);
assert.equal(nextActiveSeconds({ active_seconds: 100, last_tick_at: 990 }, 1000, 30), 110);
assert.equal(nextActiveSeconds({ active_seconds: 100, last_tick_at: 900 }, 1000, 30), 130);
assert.equal(nextActiveSeconds({ active_seconds: 100, last_tick_at: 900 }, 1000, 0), 200);
assert.equal(nextActiveSeconds({ active_seconds: 100, last_tick_at: 1100 }, 1000, 30), 100);
assert.equal(nextActiveSeconds({ active_seconds: -5 }, 1000, 30), 0);

// sessionCostUsd: assistant messages, tool results, usage entries, and
// compaction usage all contribute; an entry without usage contributes nothing.
const entries = [
	{ type: "message", message: { role: "assistant", usage: { cost: { total: 1.25 } } } },
	{ type: "message", message: { role: "toolResult", usage: { cost: { total: 0.25 } } } },
	{ type: "usage", usage: { cost: { total: 0.5 } } },
	{ type: "compaction", usage: { cost: { total: 0.1 } } },
	{ type: "branch_summary", usage: { cost: { total: 0.05 } } },
	{ type: "message", message: { role: "user" } },
	{ type: "model_change" },
];
assert.equal(sessionCostUsd(entries), 2.15);
assert.equal(sessionCostUsd([]), 0);

// spentUsd: the session total minus the baseline, and the stored value wins.
assert.equal(spentUsd({ session_baseline_usd: 1 }, 3.5), 2.5);
assert.equal(spentUsd({ session_baseline_usd: 1, spent_usd: 4 }, 3.5), 4);
assert.equal(spentUsd({ session_baseline_usd: 5 }, 3.5), 0);
assert.equal(spentUsd({ spent_usd: 2 }, 3.5), 2);
assert.equal(spentUsd(undefined, 3.5), 0);

// evaluateBudget: a cap trips at the boundary; the wall clock wins a tie.
assert.deepEqual(evaluateBudget({ caps: {}, activeSeconds: 999, spentUsd: 999 }), {
	tripped: false,
	metric: null,
});
assert.deepEqual(evaluateBudget({ caps: { wallSeconds: 60 }, activeSeconds: 60, spentUsd: 0 }), {
	tripped: true,
	metric: "wall_seconds",
});
assert.deepEqual(evaluateBudget({ caps: { costUsd: 2 }, activeSeconds: 0, spentUsd: 1.99 }), {
	tripped: false,
	metric: null,
});
assert.deepEqual(evaluateBudget({ caps: { costUsd: 2 }, activeSeconds: 0, spentUsd: 2 }), {
	tripped: true,
	metric: "cost_usd",
});
assert.deepEqual(
	evaluateBudget({ caps: { wallSeconds: 1, costUsd: 1 }, activeSeconds: 1, spentUsd: 1 }),
	{ tripped: true, metric: "wall_seconds" },
);

// budgetWindowChanged: a newer declared window resets the counters.
assert.ok(budgetWindowChanged({ budget_started_at: 200 }, { window_started_at: 100 }));
assert.ok(!budgetWindowChanged({ budget_started_at: 100 }, { window_started_at: 100 }));
// A missing spend file is a new window, so a lost file cannot block a resume.
assert.ok(budgetWindowChanged({ budget_started_at: 100 }, undefined));
assert.ok(!budgetWindowChanged({}, { window_started_at: 100 }));
assert.ok(!budgetWindowChanged({ budget_started_at: 0 }, { window_started_at: 100 }));
