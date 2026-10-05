/**
 * Sliceme unit lifecycle tool for pi.
 *
 * Registers a native `sliceme-unit` tool that forwards to the bundled `sliceme`
 * CLI.  This is the unit lifecycle tool that campaign **workers** use
 * (`status → commit`). Workers are scoped to it by their agent `tools:`
 * allowlist, so they never see the `sliceme` coordinator tool
 * (`coordinator.ts`).  The tool shells out to the bundled CLI, so no
 * `sliceme` install on `PATH` is needed.
 *
 * Ownership is decided at plan time: a worker edits only the directories its
 * DAG node owns, and `commit` refuses paths outside them (plan conformance).
 * There is no runtime declare/lease step.
 *
 * There is no automatic single-agent bootstrap: a session is only bound to a
 * unit when something explicitly creates one (the `sliceme` coordinator tool's
 * `spawn`, or a human running `sliceme start`).  The tool resolves the unit from
 * `ctx.cwd`, so a worker launched inside its unit worktree needs no `--unit`.
 *
 * The tool registers inactive; `/sliceme` activates it for the session, and the
 * coordinator's `session_start` hook re-activates it when a suspended campaign
 * is resumed.  Workers are scoped to it by their `tools:` allowlist
 * (`pi --tools sliceme-unit`); the coordinator session normally uses the
 * `sliceme` tool instead.
 */

import { StringEnum } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { runSliceme } from "./common.ts";

/** Mirrors `sliceme.surface.ACTIONS`; kept in sync by a test. */
export const SLICEME_ACTIONS = [
	"start",
	"status",
	"deliver",
	"exec",
	"wave",
	"review",
	"attempt",
	"progress",
] as const;

/** The `sliceme` tool exposes exactly the agent surface. */
export const SLICEME_TOOL_ACTIONS = [...SLICEME_ACTIONS] as const;

function toArgs(action: string, params: Record<string, unknown>): string[] {
	const args = [action];
	for (const [key, value] of Object.entries(params)) {
		if (key === "action" || value === undefined || value === null) continue;
		const flag = `--${key.replace(/_/g, "-")}`;
		if (typeof value === "boolean") {
			if (value) args.push(flag);
		} else if (Array.isArray(value)) {
			for (const item of value) args.push(flag, String(item));
		} else {
			args.push(flag, String(value));
		}
	}
	return args;
}

export default function unitExtension(pi: ExtensionAPI) {
	pi.registerTool({
		name: "sliceme-unit",
		label: "Sliceme unit",
		description:
			"Sliceme unit lifecycle for campaign workers: edit only the directories your " +
			"node owns, then `commit`. The coordinator owns the campaign and delivers work " +
			"with `deliver` only after every wave is approved.",
		promptSnippet: "Drive the Sliceme unit lifecycle (edit owned dirs → commit)",
		promptGuidelines: [
			"Edit only files inside the directories your DAG node owns; `commit` rejects paths outside them.",
			"Use `sliceme-unit` action `status` with `short: true` to confirm you are inside your unit worktree.",
			"Never run `deliver` or `git merge` yourself; the coordinator owns delivery.",
		],
		// Inactive until `/sliceme` activates it; workers are scoped to it through
		// their `tools:` allowlist (`pi --tools sliceme-unit`).
		defaultActive: false,
		parameters: Type.Object({
			action: StringEnum(SLICEME_TOOL_ACTIONS),
			unit: Type.Optional(Type.String({ description: "unit (defaults to this worktree)" })),
			task: Type.Optional(Type.String()),
			summary: Type.Optional(Type.String()),
			message: Type.Optional(Type.String({ description: "wave: commit message suffix" })),
			no_unit: Type.Optional(
				Type.Boolean({ description: "start: initialise the plane without a unit for cwd" }),
			),
			main: Type.Optional(
				Type.String({
					description: "start: integration branch to adopt (default: current; must exist)",
				}),
			),
			base: Type.Optional(Type.String({ description: "start: base branch/ref" })),
			target: Type.Optional(
				Type.String({
					description: "start/deliver: target (feature) branch; never main or master",
				}),
			),
			target_mode: Type.Optional(
				StringEnum(["current", "existing", "new"] as const, {
					description: "start: how to resolve the target branch",
				}),
			),
			source: Type.Optional(
				Type.String({
					description: "deliver: campaign worktree branch; exec: fingerprint source",
				}),
			),
			ff: Type.Optional(
				Type.Boolean({ description: "deliver: allow a fast-forward instead of a merge commit" }),
			),
			cleanup: Type.Optional(
				StringEnum(["none", "worktrees", "all"] as const, {
					description: "deliver: post-merge cleanup (default none)",
				}),
			),
			narrative: Type.Optional(
				Type.String({ description: "report: what-changed/risks narrative" }),
			),
			design: Type.Optional(Type.String({ description: "report: design document reference" })),
			simulate: Type.Optional(Type.Boolean({ description: "status: plan waves" })),
			health: Type.Optional(Type.Boolean({ description: "status: check plane health" })),
			gc: Type.Optional(Type.Boolean({ description: "status: prune landed worktrees" })),
			short: Type.Optional(Type.Boolean({ description: "status: print only the unit name" })),
			no_checks: Type.Optional(
				Type.Boolean({ description: "skip verification or delivery checks" }),
			),
			submit: Type.Optional(Type.Boolean({ description: "exec: enqueue a check job" })),
			validate: Type.Optional(
				Type.Boolean({ description: "exec: resolve and validate the sandbox gate" }),
			),
			gpu_required: Type.Optional(
				Type.Boolean({ description: "exec: with validate, require a GPU runner" }),
			),
			run: Type.Optional(Type.Boolean({ description: "exec: drain the queue" })),
			open: Type.Optional(
				Type.Boolean({ description: "wave: create or reuse the campaign worktree" }),
			),
			record: Type.Optional(
				Type.Boolean({ description: "wave: record a wave (conformance + per-node commits)" }),
			),
			wait: Type.Optional(Type.Boolean({ description: "exec: wait for a job" })),
			cancel: Type.Optional(Type.Boolean({ description: "exec: cancel a queued job" })),
			job: Type.Optional(Type.String({ description: "exec: job id" })),
			commit: Type.Optional(Type.String({ description: "exec: commit/ref to run checks at" })),
			command: Type.Optional(
				Type.Array(Type.String(), { description: "exec: check command (repeatable)" }),
			),
			sandbox: Type.Optional(
				StringEnum(["none", "bwrap", "unshare"] as const, {
					description: "exec: sandbox mode",
				}),
			),
			gpu: Type.Optional(
				StringEnum(["none", "T1", "T2"] as const, { description: "exec: GPU tier" }),
			),
			priority: Type.Optional(Type.Number({ description: "exec: higher runs first" })),
			timeout: Type.Optional(Type.Number({ description: "exec: per-command timeout seconds" })),
			wave: Type.Optional(Type.Number({ description: "exec: campaign wave" })),
			requester: Type.Optional(Type.String({ description: "exec: verifier id" })),
			limit: Type.Optional(Type.Number({ description: "exec: max jobs to drain" })),
			poll: Type.Optional(
				Type.Boolean({ description: "review: print open comments and the newest decision" }),
			),
			ack: Type.Optional(Type.Boolean({ description: "review: acknowledge one comment" })),
			state: Type.Optional(Type.Boolean({ description: "review: print one snapshot" })),
			diff: Type.Optional(Type.Boolean({ description: "review: print one file diff" })),
			comment: Type.Optional(Type.Boolean({ description: "review: record a comment" })),
			decision: Type.Optional(
				StringEnum(["approve", "request_changes", "override"] as const, {
					description: "review: record a decision",
				}),
			),
			all: Type.Optional(
				Type.Boolean({ description: "review: with approve, approve every unapproved commit" }),
			),
			file: Type.Optional(Type.String({ description: "review: file path" })),
			side: Type.Optional(StringEnum(["old", "new"] as const, { description: "review: comment side" })),
			line: Type.Optional(Type.Number({ description: "review: line number" })),
			line_end: Type.Optional(Type.Number({ description: "review: end line for a range" })),
			body: Type.Optional(Type.String({ description: "review: comment body" })),
			note: Type.Optional(Type.String({ description: "review: decision note" })),
			actor: Type.Optional(Type.String({ description: "review: who recorded the decision" })),
			comment_id: Type.Optional(Type.Number({ description: "review: comment id for --ack" })),
		}),
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const { action, ...rest } = params as Record<string, unknown> & { action: string };
			const { text, json } = await runSliceme(pi, ctx, toArgs(action, rest), signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});
}
