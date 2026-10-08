/**
 * Shared helpers for the Sliceme pi extension.
 *
 * `coordinator.ts` is the `sliceme` extension: it registers the `sliceme` engine
 * tool, the Sliceme agent definitions, and the `sliceme.campaign` workflow
 * resource, and it writes the suspend/resume descriptor. These helpers keep the
 * engine invocation, the campaign file paths, and the pi-subagents loader in
 * one place so the extension stays a thin adapter over the bundled `sliceme`
 * CLI.
 */

import { existsSync } from "node:fs";
import * as fs from "node:fs";
import { createRequire } from "node:module";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

export interface SlicemeResult {
	text: string;
	json: any;
}

export interface SlicemeInvocation {
	command: string;
	prefix: string[];
}

const HERE = (() => {
	try {
		return path.dirname(fileURLToPath(import.meta.url));
	} catch {
		return process.cwd();
	}
})();

/** The package root (the directory containing `bin/`, `integrations/`, `docs/`). */
export function packageDir(): string {
	let dir = HERE;
	for (let i = 0; i < 6; i++) {
		if (existsSync(path.join(dir, "bin", "sliceme"))) return dir;
		const parent = path.dirname(dir);
		if (parent === dir) break;
		dir = parent;
	}
	return path.resolve(HERE, "..", "..");
}

/** The pi agent directory that holds installed pi packages and agents. */
export function agentDir(): string {
	return (
		process.env.PI_CODING_AGENT_DIR ||
		process.env.PI_AGENT_DIR ||
		path.join(os.homedir(), ".pi", "agent")
	);
}

/**
 * Resolve how to run the bundled CLI. `SLICEME_BIN` overrides for local
 * development; otherwise the CLI shipped inside the pi package is used, so no
 * `PATH` install is needed. Running the bundled script through `python3` keeps
 * it portable.
 */
export function resolveSlicemeInvocation(): SlicemeInvocation {
	if (process.env.SLICEME_BIN) {
		return { command: process.env.SLICEME_BIN, prefix: [] };
	}
	const bundled = path.join(packageDir(), "bin", "sliceme");
	return { command: "python3", prefix: [bundled] };
}

export function parseJson(text: string): any {
	try {
		return JSON.parse(text);
	} catch {
		return undefined;
	}
}

/** Run the Sliceme CLI with `--json` from the session's cwd. */
export async function runSliceme(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	args: string[],
	signal?: AbortSignal,
	timeout = 600_000,
): Promise<SlicemeResult> {
	const invocation = resolveSlicemeInvocation();
	const result = await pi.exec(invocation.command, [...invocation.prefix, "--json", ...args], {
		cwd: ctx.cwd,
		signal,
		timeout,
	});
	const text = [result.stdout, result.stderr].filter((s) => s?.trim()).join("\n").trim();
	if (result.code !== 0) {
		throw new Error(text || `sliceme exited with code ${result.code}`);
	}
	return { text: text || "ok", json: parseJson(result.stdout) };
}

// ---------------------------------------------------------------------------
// Campaign paths (`docs/reference.md` §3, `docs/sessions.md` §4)
// ---------------------------------------------------------------------------

export function branchKey(branch: string): string {
	return (branch || "main").trim().replace(/\//g, "--") || "main";
}

export function stateDir(cwd: string): string {
	return path.join(cwd, ".sliceme");
}

/** The adapter-written suspend/resume descriptor (`docs/sessions.md`). */
export function sessionPath(cwd: string, branch: string): string {
	return path.join(stateDir(cwd), `${branchKey(branch)}.session.json`);
}

/** The cooperative pause flag for a campaign. */
export function controlPath(cwd: string, branch: string): string {
	return path.join(stateDir(cwd), `${branchKey(branch)}.control.json`);
}

/**
 * The private per-process pointer to the session's active campaign branch.
 *
 * The pi session owns a campaign, and a session can outlive one process, so the
 * pointer is best-effort. The engine rebuilds campaign state from git and
 * SQLite, so a missing pointer never blocks a resume.
 */
export function activeCampaignPath(cwd: string, pid: number = process.pid): string {
	return path.join(stateDir(cwd), `active.${pid}.campaign`);
}

/** Read the active campaign branch from the pointer file, if any. */
export function readActiveCampaign(
	cwd: string,
	pid: number = process.pid,
): string | undefined {
	const value = readJson<any>(activeCampaignPath(cwd, pid), undefined);
	const branch = value?.campaign ?? value?.branch;
	return typeof branch === "string" && branch ? branch : undefined;
}

/** Write the active campaign branch to the pointer file. */
export function writeActiveCampaign(
	cwd: string,
	branch: string,
	pid: number = process.pid,
): void {
	writeJson(activeCampaignPath(cwd, pid), {
		campaign: branch,
		pid,
		updated_at: Date.now() / 1000,
	});
}

/** Remove the active campaign pointer file. */
export function clearActiveCampaign(cwd: string, pid: number = process.pid): void {
	try {
		fs.rmSync(activeCampaignPath(cwd, pid), { force: true });
	} catch {
		/* the file may already be gone */
	}
}

export function readJson<T>(file: string, fallback: T): T {
	try {
		return JSON.parse(fs.readFileSync(file, "utf8")) as T;
	} catch {
		return fallback;
	}
}

/** Write a JSON file atomically: a sibling temporary file, then a rename. */
export function writeJson(file: string, data: unknown): void {
	fs.mkdirSync(path.dirname(file), { recursive: true });
	const tmp = `${file}.${process.pid}.${Date.now()}.tmp`;
	fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + "\n", "utf8");
	fs.renameSync(tmp, file);
}

// ---------------------------------------------------------------------------
// Engine reply handling (`docs/sessions.md`)
// ---------------------------------------------------------------------------

/**
 * Resolve the campaign branch from an engine reply.
 *
 * ``status``, ``start``, and ``deliver`` return ``feature_branch`` (with the
 * deprecated ``target_branch`` / ``main_branch`` mirrors).  Delivery marks the
 * descriptor, so it must resolve the branch from the deliver reply shape too.
 */
export function replyBranch(json: any): string | undefined {
	const branch = json?.feature_branch ?? json?.target_branch ?? json?.main_branch;
	return typeof branch === "string" && branch ? branch : undefined;
}

/** Whether a ``deliver`` reply reports a landed delivery (`docs/sessions.md` §6). */
export function delivered(json: any): boolean {
	const results = json?.results;
	return (
		Array.isArray(results) &&
		results.length > 0 &&
		results.every((result: any) => result?.status === "landed")
	);
}

/**
 * Mark the session descriptor ``completed`` after a successful delivery.
 *
 * Shutdown and resume read the descriptor status, so the marker must land
 * before the process ends.  This merges the status into the existing
 * descriptor and keeps its other fields.
 */
export function markDescriptorCompleted(cwd: string, branch: string): void {
	try {
		const file = sessionPath(cwd, branch);
		const existing = readJson<any>(file, {});
		writeJson(file, {
			...existing,
			feature_branch: branch,
			status: "completed",
			completed_at: Date.now() / 1000,
		});
	} catch {
		/* Best-effort marker; the engine store still records `delivered`. */
	}
}

/**
 * Apply one engine reply to the adapter files.
 *
 * The active-campaign pointer follows the reply's branch.  A successful
 * ``deliver`` marks the descriptor ``completed`` regardless of the pointer
 * guard, because the deliver reply carries ``feature_branch`` and may arrive
 * before any pointer exists.
 */
export function applyEngineReply(cwd: string, action: string, json: any): string | undefined {
	const branch = replyBranch(json);
	if (branch) writeActiveCampaign(cwd, branch);
	if (action === "deliver" && delivered(json)) {
		const target = branch ?? readActiveCampaign(cwd);
		if (target) markDescriptorCompleted(cwd, target);
	}
	return branch;
}

// ---------------------------------------------------------------------------
// pi-subagents
// ---------------------------------------------------------------------------

/**
 * Candidate install roots of the `pi-subagents` pi package.
 *
 * A pi package installed next to Sliceme is not automatically a Node
 * dependency of this package, so the loader discovers it instead of relying on
 * a bare import. `SLICEME_PI_SUBAGENTS` overrides the search for local
 * development.
 */
export function piSubagentsRoots(): string[] {
	const roots: string[] = [];
	if (process.env.SLICEME_PI_SUBAGENTS) roots.push(path.resolve(process.env.SLICEME_PI_SUBAGENTS));
	roots.push(path.join(packageDir(), "node_modules", "pi-subagents"));
	roots.push(path.join(agentDir(), "npm", "node_modules", "pi-subagents"));
	return roots;
}

/**
 * Import one subpath of the installed `pi-subagents` package, or `undefined`
 * when it is not installed. The resource loader imports the
 * `workflow-resources` subpath; the import is dynamic because TypeScript must
 * not require the package at build time and the runtime must not fail to load
 * the extension without it.
 */
export async function loadPiSubagents(subpath: string): Promise<any | undefined> {
	for (const root of piSubagentsRoots()) {
		if (!existsSync(path.join(root, "package.json"))) continue;
		try {
			const require = createRequire(path.join(root, "package.json"));
			const resolved = require.resolve(`pi-subagents/${subpath}`);
			return await import(pathToFileURL(resolved).href);
		} catch {
			/* try the next install root */
		}
	}
	try {
		return await import(`pi-subagents/${subpath}`);
	} catch {
		return undefined;
	}
}
