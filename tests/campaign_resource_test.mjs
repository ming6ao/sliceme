/**
 * Unit tests for the `sliceme.campaign` workflow resource in
 * `integrations/pi/campaign-resource.ts`.
 *
 * The resolver is pure, so this harness pins the fixed host commands, the
 * bounded-field validation, and the campaign-token rejection without a pi
 * runtime. The last section runs the generated script against a fake `runs`
 * object, so it also pins the host-output guard and the bounded reviewer
 * evidence. `tests/test_pi_package.py` runs it through Node's type stripping.
 */
import assert from "node:assert/strict";
import {
	CAMPAIGN_RESOURCE,
	CAMPAIGN_RESOURCE_FIELDS,
	CAMPAIGN_RESOURCE_VERSION,
	CAMPAIGN_TOKEN,
	EVIDENCE_LIMIT,
	HOST_CUT_MARKER,
	HOST_PREVIEW_BYTES,
	campaignCommands,
	resolveCampaignResource,
} from "../integrations/pi/campaign-resource.ts";

const ENGINE = ["python3", "/opt/sliceme/bin/sliceme"];

assert.equal(CAMPAIGN_RESOURCE, "sliceme.campaign");
assert.equal(CAMPAIGN_RESOURCE_VERSION, 2);
assert.equal(HOST_PREVIEW_BYTES, 4096);
assert.equal(HOST_CUT_MARKER, "...");
assert.deepEqual([...CAMPAIGN_RESOURCE_FIELDS].sort(), ["campaign", "nodeCap", "phaseCap"]);
assert.ok(CAMPAIGN_TOKEN.test("feat.x"));
assert.ok(!CAMPAIGN_TOKEN.test("feat/x"));
assert.ok(!CAMPAIGN_TOKEN.test(""));

// The four fixed host commands, built from literals plus the engine path. The
// loop reads the brief form: the host returns at most 4096 bytes of standard
// output, and one detailed node definition can exceed that.
assert.deepEqual(campaignCommands(ENGINE), {
	status: "python3 /opt/sliceme/bin/sliceme --json status",
	ready: "python3 /opt/sliceme/bin/sliceme --json ready --brief",
	record: "python3 /opt/sliceme/bin/sliceme --json record",
	check: "python3 /opt/sliceme/bin/sliceme --json check",
});

// The campaign token is the only variable text and is bound with --campaign.
const withCampaign = campaignCommands(ENGINE, "feat.x");
assert.equal(
	withCampaign.status,
	"python3 /opt/sliceme/bin/sliceme --json status --campaign feat.x",
);
assert.equal(
	withCampaign.record,
	"python3 /opt/sliceme/bin/sliceme --json record --campaign feat.x",
);

// A full resolution exposes the four grants and a script that uses the state
// commands.
const resolved = resolveCampaignResource({ campaign: "feat.x", phaseCap: 3, nodeCap: 9 }, ENGINE);
assert.ok(!("error" in resolved), JSON.stringify(resolved));
const expectedKeys = ["status"];
for (let index = 0; index < 3; index++) {
	expectedKeys.push(`ready-${index}`, `record-${index}`, `check-${index}`);
}
assert.deepEqual(
	resolved.hostCommands.map((grant) => grant.key),
	expectedKeys,
);

// The resource cannot approve or deliver: the user permission is the only gate.
assert.ok(!resolved.hostCommands.some((grant) => grant.key === "approve"));
assert.ok(!resolved.hostCommands.some((grant) => grant.key === "deliver"));

// No `open` host grant: the coordinator opens the worktree with
// `sliceme open` before it starts the resource (plan correction 4).
assert.ok(!resolved.hostCommands.some((grant) => grant.key === "open"));
assert.ok(!resolved.script.includes("--open"));
for (const key of ["status", "ready-0", "record-0", "check-0"]) {
	const grant = resolved.hostCommands.find((entry) => entry.key === key);
	assert.ok(grant, `missing grant ${key}`);
	assert.ok(
		resolved.script.includes(JSON.stringify(grant.command)),
		`the script does not call the granted ${key} command`,
	);
}
assert.ok(resolved.script.includes('"sliceme-worker"'));
// The worker starts in the campaign worktree and reads its own node from the
// plan file. The brief reply carries the paths, because the workflow sandbox
// cannot read `.sliceme/` and the host cuts standard output at 4096 bytes.
assert.ok(resolved.script.includes("ready.worktree"));
assert.ok(resolved.script.includes("ready.nodes"));
assert.ok(resolved.script.includes("ready.dag"));
assert.ok(resolved.script.includes("cwd: worktree"));
assert.ok(resolved.script.includes("Worktree: "));
assert.ok(resolved.script.includes("Owned directory: "));
assert.ok(resolved.script.includes("Plan file: "));
assert.ok(resolved.script.includes("Read no other file under .sliceme/"));
// The inline goal and tasks are gone: one long node would cross the cap.
assert.ok(!resolved.script.includes("Goal: "));
assert.ok(!resolved.script.includes("Tasks:"));
// Every host reply passes the guard before it is parsed or reviewed.
assert.ok(resolved.script.includes('hostJson(readyRes, "ready")'));
assert.ok(resolved.script.includes('hostJson(statusRes, "status")'));
assert.ok(resolved.script.includes('hostEvidence("record", recordRes)'));
assert.ok(resolved.script.includes('hostEvidence("check", checkRes)'));
assert.ok(resolved.script.includes("status_error"));
// A paused ready carries the engine's pause reason, so a budget stop and a
// user suspension stay distinct.
assert.ok(resolved.script.includes('state: "paused"'));
assert.ok(resolved.script.includes("ready.pause_reason"));
assert.ok(resolved.script.includes("ready.pause_metric"));
assert.ok(resolved.script.includes('"ready-" + phases'));
assert.ok(resolved.script.includes('"record-" + phases'));
assert.ok(resolved.script.includes('"check-" + phases'));
assert.ok(resolved.script.includes('"reviewer"'));
assert.ok(resolved.script.includes("const PHASE_CAP = 3;"));
assert.ok(resolved.script.includes("const NODE_CAP = 9;"));

// An omitted bound falls back to the bounded default (10 phases / 256 nodes).
// The host accepts at most 32 grants, and each phase needs three plus the
// final status grant: 3 * 10 + 1 = 31.
const defaulted = resolveCampaignResource({}, ENGINE);
assert.ok(!("error" in defaulted));
assert.ok(defaulted.script.includes("const PHASE_CAP = 10;"));
assert.ok(defaulted.script.includes("const NODE_CAP = 256;"));
assert.equal(defaulted.hostCommands.length, 31);
assert.ok(defaulted.script.includes('state: "capped"'));
assert.ok(defaulted.script.includes("run the sliceme.campaign resource again"));

// Unknown fields are rejected.
const unknown = resolveCampaignResource({ task: "review this" }, ENGINE);
assert.ok("error" in unknown && unknown.error.includes("unsupported fields: task"));

// The campaign token is strict.
for (const campaign of ["feat/x", "", "a b", "x".repeat(129)]) {
	const bad = resolveCampaignResource({ campaign }, ENGINE);
	assert.ok("error" in bad, `accepted campaign ${JSON.stringify(campaign)}`);
}

// The caps are bounded. The phase cap is 10 because of the host grant limit.
for (const phaseCap of [0, 11, 1.5, "3"]) {
	assert.ok("error" in resolveCampaignResource({ phaseCap }, ENGINE), String(phaseCap));
}
const atLimit = resolveCampaignResource({ phaseCap: 10 }, ENGINE);
assert.ok(!("error" in atLimit));
assert.equal(atLimit.hostCommands.length, 31);
const overLimit = resolveCampaignResource({ phaseCap: 11 }, ENGINE);
assert.ok("error" in overLimit && overLimit.error.includes("1 to 10"));
for (const nodeCap of [0, 257, 1.5, "3"]) {
	assert.ok("error" in resolveCampaignResource({ nodeCap }, ENGINE), String(nodeCap));
}

// A missing engine path fails closed.
assert.ok("error" in resolveCampaignResource({}, []));

// --- The loop against a fake runtime -----------------------------------------
//
// The generated script is plain JavaScript over `runs.host`, `runs.all`, and
// `runs.run`, so it runs here without pi-subagents. These cases pin the two
// host-output rules of the loop: a cut reply reports itself, and a cut evidence
// payload never reaches a reviewer as complete evidence.

const PLAN = "/root/.sliceme/feat.x.dag.json";
const WORKTREE = "/root/.sliceme/worktrees/feat-x";

function hostOk(key, stdout, extra = {}) {
	return {
		key,
		kind: "command",
		ok: true,
		state: "passed",
		exitCode: 0,
		stdout,
		stderr: "",
		outputPath: `/tmp/run/${key}.log`,
		durationMs: 1,
		...extra,
	};
}

function fakeRuns(hostPlan) {
	const calls = { host: [], run: [], all: [] };
	const runs = {
		host(key, params) {
			calls.host.push({ key, params });
			const result = hostPlan[key];
			if (!result) throw new Error(`the script called an unexpected host key: ${key}`);
			return Promise.resolve(result);
		},
		run(key, params) {
			calls.run.push({ key, params });
			return Promise.resolve({ key, ok: true });
		},
		all(items) {
			calls.all.push(items);
			return Promise.resolve(items.map((item) => ({ key: item.key, ok: true })));
		},
	};
	return { runs, calls };
}

function builtScript(args) {
	const resolved = resolveCampaignResource(args, ENGINE);
	assert.ok(!("error" in resolved), JSON.stringify(resolved));
	return new Function("runs", `return (async () => {\n${resolved.script}\n})()`);
}

// A cut `ready` reply stops the loop with the verb, the sizes, the cap, and the
// capture path. A parser must never see the cut text as complete JSON.
{
	const cut = `{"campaign": "feat.x", "nodes": ["${"x".repeat(HOST_PREVIEW_BYTES)}${HOST_CUT_MARKER}`;
	const { runs, calls } = fakeRuns({ "ready-0": hostOk("ready-0", cut) });
	await assert.rejects(builtScript({ campaign: "feat.x" })(runs), (error) => {
		assert.match(error.message, /sliceme ready output is incomplete/);
		assert.match(error.message, /4096 bytes/);
		assert.ok(error.message.includes("/tmp/run/ready-0.log"), error.message);
		return true;
	});
	assert.deepEqual(calls.all, []);
}

// A small ready reply starts one worker per node. The worker task names the
// plan file and the owned directory, and it carries no inline node text.
{
	const { runs, calls } = fakeRuns({
		"ready-0": hostOk(
			"ready-0",
			JSON.stringify({
				campaign: "feat.x",
				ready: ["n0"],
				nodes: [{ id: "n0", dir: "src/api", label: "api" }],
				dag: PLAN,
				worktree: WORKTREE,
				phase: 0,
				paused: false,
			}),
		),
		"ready-1": hostOk(
			"ready-1",
			JSON.stringify({ campaign: "feat.x", ready: [], nodes: [], dag: PLAN, worktree: WORKTREE, phase: 1, paused: false }),
		),
		"record-0": hostOk("record-0", "recorded n0 -> abc1234"),
		"check-0": hostOk("check-0", "[passed] ok: true"),
		status: hostOk("status", JSON.stringify({ campaign: "feat.x", ready: [] })),
	});
	const result = await builtScript({ campaign: "feat.x" })(runs);
	assert.equal(result.state, "complete");
	assert.equal(result.phases, 1);
	assert.equal(result.nodes, 1);
	assert.equal(result.status.campaign, "feat.x");
	assert.equal(result.status_error, null);
	assert.equal(calls.all.length, 1);
	const worker = calls.all[0][0];
	assert.equal(worker.key, "node-n0");
	assert.equal(worker.agent, "sliceme-worker");
	assert.equal(worker.cwd, WORKTREE);
	assert.ok(worker.task.includes(`Plan file: ${PLAN}`), worker.task);
	assert.ok(worker.task.includes("Owned directory: src/api"), worker.task);
	assert.ok(!worker.task.includes("Goal: "), worker.task);
	const reviewer = calls.run.find((call) => call.params.agent === "reviewer");
	assert.ok(reviewer.params.task.includes("recorded n0 -> abc1234"));
	assert.ok(reviewer.params.task.includes("[passed] ok: true"));
	assert.ok(!reviewer.params.task.includes("output cut"), "a complete reply needs no cut note");
}

// Evidence larger than the reviewer budget, or cut by the host, arrives with
// both counts and the capture path, and the inline text stays bounded.
{
	const long = "y".repeat(EVIDENCE_LIMIT + 500);
	const cut = `[failed] noisy: python3 -c "..."\n${"z".repeat(64)}${HOST_CUT_MARKER}`;
	const { runs, calls } = fakeRuns({
		"ready-0": hostOk(
			"ready-0",
			JSON.stringify({
				campaign: "feat.x",
				ready: ["n0", "n1"],
				nodes: [
					{ id: "n0", dir: "src/api" },
					{ id: "n1", dir: "src/core" },
				],
				dag: PLAN,
				worktree: WORKTREE,
				phase: 0,
				paused: false,
			}),
		),
		"ready-1": hostOk(
			"ready-1",
			JSON.stringify({ campaign: "feat.x", ready: [], nodes: [], dag: PLAN, worktree: WORKTREE, phase: 1, paused: false }),
		),
		"record-0": hostOk("record-0", cut),
		"check-0": hostOk("check-0", long),
		status: hostOk("status", JSON.stringify({ campaign: "feat.x" })),
	});
	const result = await builtScript({ campaign: "feat.x" })(runs);
	assert.equal(result.state, "complete");
	const reviewer = calls.run.find((call) => call.params.agent === "reviewer");
	const task = reviewer.params.task;
	assert.ok(task.includes(`[record output cut: received ${cut.length} characters; full output: /tmp/run/record-0.log]`), task);
	assert.ok(task.includes(`[check output cut: received ${long.length} characters; full output: /tmp/run/check-0.log]`), task);
	assert.ok(!task.includes("y".repeat(EVIDENCE_LIMIT + 1)), "the inline check text stays bounded");
	assert.ok(task.length < 2 * EVIDENCE_LIMIT + 2000, `the reviewer task stays bounded: ${task.length}`);
}

// A cut `status` reply does not become a string in the result. The loop completes
// with `status: null` and a `status_error` that names the cut.
{
	const { runs } = fakeRuns({
		"ready-0": hostOk(
			"ready-0",
			JSON.stringify({ campaign: "feat.x", ready: [], nodes: [], dag: PLAN, worktree: WORKTREE, phase: 0, paused: false }),
		),
		status: hostOk("status", `{"campaign": "feat.x", "ready": ["${"q".repeat(64)}${HOST_CUT_MARKER}`),
	});
	const result = await builtScript({ campaign: "feat.x" })(runs);
	assert.equal(result.state, "complete");
	assert.equal(result.status, null);
	assert.match(result.status_error, /sliceme status output is incomplete/);
	assert.match(result.status_error, /4096 bytes/);
}

// A paused ready reply stops the loop before any worker starts.
{
	const { runs, calls } = fakeRuns({
		"ready-0": hostOk(
			"ready-0",
			JSON.stringify({
				campaign: "feat.x",
				ready: ["n0"],
				nodes: [{ id: "n0", dir: "src/api" }],
				dag: PLAN,
				worktree: WORKTREE,
				phase: 0,
				paused: true,
				pause_reason: "budget",
				pause_metric: "wall-clock",
			}),
		),
	});
	const result = await builtScript({ campaign: "feat.x" })(runs);
	assert.equal(result.state, "paused");
	assert.equal(result.reason, "budget");
	assert.equal(result.metric, "wall-clock");
	assert.deepEqual(calls.all, []);
}
