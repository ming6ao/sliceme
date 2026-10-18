/**
 * The campaign budget: caps, spend accounting, and the trip test.
 *
 * This module is import-free and pure, so a Node harness tests it without a pi
 * runtime (`tests/budget_test.mjs`). The extension `coordinator.ts` owns the
 * files and the timer. The engine only stores the caps in `.sliceme/config.json`
 * and reports the pause reason (`docs/sessions.md`).
 *
 * The wall clock counts **active campaign seconds**. The extension adds one
 * tick delta at a time, so a closed process or a suspension does not consume
 * the cap. The delta is clamped, so a crash gap cannot consume the cap either.
 *
 * The cost is the pi session cost in USD, measured from the session baseline
 * that the campaign recorded. That number includes the parent conversation,
 * the planners, the reviewers, and compaction. It is the cost the user pays.
 */

/** The two optional caps. An empty object means "no cap". */
export interface BudgetCaps {
	wallSeconds?: number;
	costUsd?: number;
}

/** The adapter-written spend file (`.sliceme/<branch-key>.budget.json`). */
export interface BudgetSpend {
	/** The `budget_started_at` value the counters belong to. */
	window_started_at?: number;
	/** The accumulated active campaign seconds. */
	active_seconds?: number;
	/** The last tick time in seconds. */
	last_tick_at?: number;
	/** The highest observed campaign cost in USD. */
	spent_usd?: number;
	/** The session cost when the measurement window opened. */
	session_baseline_usd?: number;
}

export type BudgetMetric = "wall_seconds" | "cost_usd";

export interface BudgetVerdict {
	tripped: boolean;
	metric: BudgetMetric | null;
}

/** Read the caps from a parsed `.sliceme/config.json`. Invalid values drop out. */
export function readBudgetCaps(config: unknown): BudgetCaps {
	const raw = (config as { budget?: unknown } | null | undefined)?.budget;
	if (!raw || typeof raw !== "object") return {};
	const caps: BudgetCaps = {};
	const wall = Number((raw as { wall_seconds?: unknown }).wall_seconds);
	if (Number.isFinite(wall) && wall > 0) caps.wallSeconds = wall;
	const usd = Number((raw as { cost_usd?: unknown }).cost_usd);
	if (Number.isFinite(usd) && usd > 0) caps.costUsd = usd;
	return caps;
}

/** Whether at least one cap exists. */
export function hasCampaignBudget(caps: BudgetCaps): boolean {
	return caps.wallSeconds !== undefined || caps.costUsd !== undefined;
}

/**
 * The active seconds after one tick.
 *
 * The delta starts at `last_tick_at`, or at `startSeconds` on the first tick.
 * The clamp keeps a crash gap out of the count. A `maxDeltaSeconds` of zero
 * disables the clamp.
 */
export function nextActiveSeconds(
	spend: BudgetSpend | undefined,
	nowSeconds: number,
	maxDeltaSeconds: number,
	startSeconds?: number,
): number {
	const base = Number(spend?.active_seconds ?? 0);
	const safeBase = Number.isFinite(base) && base > 0 ? base : 0;
	const last = Number(spend?.last_tick_at ?? startSeconds ?? nowSeconds);
	if (!Number.isFinite(last)) return safeBase;
	let delta = nowSeconds - last;
	if (delta < 0) delta = 0;
	if (maxDeltaSeconds > 0 && delta > maxDeltaSeconds) delta = maxDeltaSeconds;
	return safeBase + delta;
}

/**
 * The reported session cost in USD.
 *
 * Usage appears on assistant and tool-result messages, on `usage` entries, and
 * on compaction and branch-summary entries. Tools that run nested models add
 * their usage to the calling tool result, so the sum has no double count.
 */
export function sessionCostUsd(entries: readonly unknown[]): number {
	let total = 0;
	for (const entry of entries ?? []) {
		if (!entry || typeof entry !== "object") continue;
		const record = entry as {
			type?: string;
			message?: { usage?: { cost?: { total?: unknown } } };
			usage?: { cost?: { total?: unknown } };
		};
		const usage = record.type === "message" ? record.message?.usage : record.usage;
		const value = Number(usage?.cost?.total);
		if (Number.isFinite(value) && value > 0) total += value;
	}
	return total;
}

/**
 * The campaign cost in USD.
 *
 * The session total minus the baseline is the campaign cost. The stored value
 * wins when it is larger, so a new session without the baseline cannot lower
 * the visible spend.
 */
export function spentUsd(spend: BudgetSpend | undefined, sessionCost: number): number {
	const stored = Number(spend?.spent_usd ?? 0);
	const safeStored = Number.isFinite(stored) && stored > 0 ? stored : 0;
	const baseline = spend?.session_baseline_usd;
	if (typeof baseline !== "number" || !Number.isFinite(baseline)) return safeStored;
	const measured = sessionCost - baseline;
	return Math.max(safeStored, measured > 0 ? measured : 0);
}

/** Test the caps. A cap trips when the spend reaches it. */
export function evaluateBudget(args: {
	caps: BudgetCaps;
	activeSeconds: number;
	spentUsd: number;
}): BudgetVerdict {
	if (args.caps.wallSeconds !== undefined && args.activeSeconds >= args.caps.wallSeconds) {
		return { tripped: true, metric: "wall_seconds" };
	}
	if (args.caps.costUsd !== undefined && args.spentUsd >= args.caps.costUsd) {
		return { tripped: true, metric: "cost_usd" };
	}
	return { tripped: false, metric: null };
}

/**
 * Whether the caps were re-granted after the spend counters were written.
 *
 * A `budget` action sets a new `budget_started_at`, so a newer value starts a
 * fresh window: the counters reset and the pause flag can clear.
 */
export function budgetWindowChanged(
	config: unknown,
	spend: BudgetSpend | undefined,
): boolean {
	const declared = Number((config as { budget_started_at?: unknown } | null)?.budget_started_at);
	if (!Number.isFinite(declared) || declared <= 0) return false;
	const stored = Number(spend?.window_started_at ?? 0);
	return declared > stored;
}
