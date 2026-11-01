/**
 * Unit tests for the `sliceme.campaign` workflow resource in
 * `integrations/pi/campaign-resource.ts`.
 *
 * The resolver is pure, so this harness pins the fixed host commands, the
 * bounded-field validation, and the campaign-token rejection without a pi
 * runtime. The last sections run the generated script against a fake `runs`
 * object, so they also pin the host-output guard, the bounded reviewer
 * evidence, the per-child options and bindings, and the per-node retry with
 * its salvage prompt. `tests/test_pi_package.py` runs it through Node's type
 * stripping.
 */
import assert from "node:assert/strict";
import path from "node:path";
import {
	CAMPAIGN_RESOURCE,
	CAMPAIGN_RESOURCE_FIELDS,
	CAMPAIGN_RESOURCE_VERSION,
	CAMPAIGN_TOKEN,
	EVIDENCE_LIMIT,
	HOST_CUT_MARKER,
	HOST_PREVIEW_BYTES,
	WORKER_CHECKPOINT_MS,
	WORKER_NODE_TIMEOUT_MS,
	WORKER_TOOL_TIMEOUT_MS,
	campaignCommands,
	campaignGrants,
	resolveCampaignResource,
} from "../integrations/pi/campaign-resource.ts";

const ENGINE = ["python3", "/opt/sliceme/bin/sliceme"];

assert.equal(CAMPAIGN_RESOURCE, "sliceme.campaign");
assert.equal(CAMPAIGN_RESOURCE_VERSION, 3);
assert.equal(HOST_PREVIEW_BYTES, 4096);
assert.equal(HOST_CUT_MARKER, "...");
assert.equal(WORKER_TOOL_TIMEOUT_MS, 600_000);
assert.equal(WORKER_NODE_TIMEOUT_MS, 3_600_000);
assert.equal(WORKER_CHECKPOINT_MS, 180_000);
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

// A full resolution exposes the three per-phase grants and the final status
// grant, and a script that uses the state commands. The per-node retry grants
// nothing: the salvage retake resets its own directory, and the host
// authority authorizes exact fixed commands only, so a node id can never ride
// in a granted command.
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
assert.deepEqual(campaignGrants(ENGINE, "feat.x", 2).map((grant) => grant.key), [
	"status",
	"ready-0",
	"record-0",
	"check-0",
	"ready-1",
	"record-1",
	"check-1",
]);

// The resource cannot approve or deliver: the user permission is the only gate.
assert.ok(!resolved.hostCommands.some((grant) => grant.key === "approve"));
assert.ok(!resolved.hostCommands.some((grant) => grant.key === "deliver"));
assert.ok(!resolved.hostCommands.some((grant) => grant.key.startsWith("recover")));

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
assert.ok(resolved.script.includes("ready.branch_key"));
assert.ok(resolved.script.includes("cwd: worktree"));
assert.ok(resolved.script.includes("Worktree: "));
assert.ok(resolved.script.includes("Owned directory: "));
assert.ok(resolved.script.includes("Plan file: "));
assert.ok(resolved.script.includes("Read no other file under .sliceme/"));
// Every worker ends with one explicit structured status, so a silent no-op
// child is distinct from a good child.
assert.ok(resolved.script.includes("outputSchema: RESULT_STATUS"));
assert.ok(resolved.script.includes('"enum":["done","blocked"]'));
assert.ok(resolved.script.includes('structuredOutput.status === "done"'));
// The per-child deadlines: one hard per-tool deadline, one run deadline, and
// the checkpoint steer before the kill (async single-agent child fields).
assert.ok(resolved.script.includes("toolTimeoutMs: 600000"));
assert.ok(resolved.script.includes("timeoutMs: 3600000"));
assert.ok(resolved.script.includes("checkpointBeforeDeadlineMs: 180000"));
// The child carries the launch bindings, so the package extension hook knows
// the campaign, the node, and the run key (node-<id>, a retake node-<id>-try-2).
assert.ok(resolved.script.includes('"sliceme/1"'));
assert.ok(resolved.script.includes("campaign: keyOfCampaign"));
assert.ok(resolved.script.includes('run: key'));
// The bindings carry the absolute state directory, because the child runs in
// the campaign worktree and the engine's channel lives at the plane root.
assert.ok(resolved.script.includes("dir: stateDir"));
assert.ok(resolved.script.includes("stateDirOf(plan)"));
assert.ok(resolved.script.includes('return parent(parent(planPath)) + "/.sliceme"'));
assert.ok(resolved.script.includes('"node-" + job.id + "-try-2"'));
// The per-node retry observes every worker promise with a catch wrapper and
// one Promise.all (the sandbox rejects Promise.allSettled).
assert.ok(resolved.script.includes("const settled = await Promise.all(jobs);"));
assert.ok(!resolved.script.includes("allSettled"));
// The salvage prompt carries the terminal reason (bounded to the evidence
// cap) and the reset directive, and the retake reads its own bounded progress
// tail first: the brief reply names the channel once as one relative dir.
assert.ok(resolved.script.includes("ready.progress_dir"));
assert.ok(resolved.script.includes("function tailPathOf"));
assert.ok(resolved.script.includes('reason.slice(0, EVIDENCE_LIMIT)'));
assert.ok(resolved.script.includes("git restore --source=HEAD -- "));
assert.ok(resolved.script.includes("Your first run ended with: "));
assert.ok(resolved.script.includes("progress tail"));
assert.ok(resolved.script.includes("short timeout first"));
// A node that declares no directory gets no reset directive: a reset at the
// worktree root would destroy every sibling's work.
assert.ok(resolved.script.includes("if (node.dir) {"));
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
// A blocked node never fails the phase for the batch: the return names the
// node, the terminal reason, and the try count.
assert.ok(resolved.script.includes('state: "blocked"'));
assert.ok(resolved.script.includes("tries: 2"));

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
// The generated script is plain JavaScript over `runs.host`, `runs.run`, and
// `Promise.all`, so it runs here without pi-subagents. These cases pin the
// host-output rules, the per-child launch shape, and the per-node retry.

const PLAN = "/root/.sliceme/feat--x.dag.json";
const WORKTREE = "/root/.sliceme/worktrees/feat-x";
const BRANCH_KEY = "feat--x";

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

function readyReply(extra = {}) {
	return JSON.stringify({
		campaign: "feat.x",
		branch_key: BRANCH_KEY,
		// The brief payload names the channel once, relative to `.sliceme/`,
		// so a wide phase cannot cross the 4096-byte host preview cap.
		progress_dir: `${BRANCH_KEY}.progress`,
		ready: ["n0"],
		nodes: [{ id: "n0", dir: "src/api", label: "api" }],
		dag: PLAN,
		worktree: WORKTREE,
		phase: 0,
		paused: false,
		...extra,
	});
}

const WORKER_SCHEMA = {
	type: "object",
	properties: { status: { type: "string", enum: ["done", "blocked"] } },
	required: ["status"],
	additionalProperties: false,
};

function workerDone() {
	return { key: "?", ok: true, structuredOutput: { status: "done" } };
}

function fakeRuns(hostPlan, runPlan) {
	const calls = { host: [], run: [] };
	const runs = {
		host(key, params) {
			calls.host.push({ key, params });
			const result = hostPlan[key];
			if (!result) throw new Error(`the script called an unexpected host key: ${key}`);
			return Promise.resolve(result);
		},
		run(key, params) {
			calls.run.push({ key, params });
			const plan = runPlan?.[key];
			if (plan === "fail") {
				return Promise.resolve({ key, ok: false, state: "failed", error: "the run failed" });
			}
			if (plan && plan instanceof Error) return Promise.reject(plan);
			if (plan && plan.timedOutError) {
				return Promise.reject(new Error("the run timed out and was terminated"));
			}
			if (plan === "blocked") {
				return Promise.resolve({ key, ok: true, structuredOutput: { status: "blocked" } });
			}
			return Promise.resolve(workerDone());
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
	assert.deepEqual(calls.run.filter((call) => !call.key.startsWith("review-")), []);
}

// A small ready reply starts one worker per node through one keyed runs.run
// per child, with the per-child deadlines, the structured status schema, and
// the launch bindings. The worker task names the plan file and the owned
// directory, and it carries no inline node text.
{
	const { runs, calls } = fakeRuns({
		"ready-0": hostOk("ready-0", readyReply()),
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
	assert.deepEqual(result.blocked, []);
	assert.equal(result.status.campaign, "feat.x");
	assert.equal(result.status_error, null);
	const worker = calls.run.find((call) => call.key === "node-n0");
	assert.ok(worker, "the worker keys its runs.run call");
	assert.equal(worker.params.agent, "sliceme-worker");
	assert.equal(worker.params.cwd, WORKTREE);
	assert.ok(worker.params.task.includes(`Plan file: ${PLAN}`), worker.params.task);
	assert.ok(worker.params.task.includes("Owned directory: src/api"), worker.params.task);
	assert.ok(!worker.params.task.includes("Goal: "), worker.params.task);
	// The per-child launch shape: flat fields, not a nested `options` object.
	assert.equal(worker.params.toolTimeoutMs, 600_000);
	assert.equal(worker.params.timeoutMs, 3_600_000);
	assert.equal(worker.params.checkpointBeforeDeadlineMs, 180_000);
	assert.deepEqual(worker.params.outputSchema, WORKER_SCHEMA);
	assert.deepEqual(worker.params.extensionBindings, {
		// The bindings carry the state directory too: the child runs in the
		// campaign worktree, and the engine's channel lives at the plane root.
		"sliceme/1": {
			campaign: BRANCH_KEY,
			node: "n0",
			run: "node-n0",
			dir: path.dirname(path.dirname(PLAN)) + "/.sliceme",
		},
	});
	// The reviewer rides its own keyed run.
	const reviewer = calls.run.find((call) => call.params.agent === "reviewer");
	assert.ok(reviewer.params.task.includes("recorded n0 -> abc1234"));
	assert.ok(reviewer.params.task.includes("[passed] ok: true"));
	assert.ok(!reviewer.params.task.includes("output cut"), "a complete reply needs no cut note");
}

// A failed child relaunches once with a new key and a salvage prompt that
// carries the terminal reason, its progress-tail path, and the reset
// directive. A sibling that succeeds is unaffected: the phase still records
// and the run completes with no blocked node.
{
	const { runs, calls } = fakeRuns(
		{
			"ready-0": hostOk(
				"ready-0",
				readyReply({
					ready: ["n0", "n1"],
					nodes: [
						{ id: "n0", dir: "src/api" },
						{ id: "n1", dir: "src/core" },
					],
				}),
			),
			"ready-1": hostOk(
				"ready-1",
				JSON.stringify({ campaign: "feat.x", ready: [], nodes: [], dag: PLAN, worktree: WORKTREE, phase: 1, paused: false }),
			),
			"record-0": hostOk("record-0", "recorded"),
			"check-0": hostOk("check-0", "[passed] ok: true"),
			status: hostOk("status", JSON.stringify({ campaign: "feat.x" })),
		},
		{ "node-n0": new Error("the run timed out and was terminated") },
	);
	const result = await builtScript({ campaign: "feat.x" })(runs);
	// The retake of n0 succeeds, so no node stays blocked.
	assert.equal(result.state, "complete");
	assert.deepEqual(result.blocked, []);
	const retake = calls.run.find((call) => call.key === "node-n0-try-2");
	assert.ok(retake, "the failed child relaunches with a new key");
	const sibling = calls.run.filter((call) => call.key === "node-n1");
	assert.equal(sibling.length, 1, "the sibling launches exactly once");
	const task = retake.params.task;
	assert.ok(task.includes("You are retaking Sliceme DAG node 'n0'"), task);
	assert.ok(
		task.includes("Your first run ended with: the run timed out and was terminated"),
		task,
	);
	assert.ok(
		// The tail path is built from the reply's relative progress dir and the
		// child's own state directory, not a per-node absolute path.
		task.includes("read your own bounded progress tail: /root/.sliceme/feat--x.progress/n0.jsonl"),
		task,
	);
	assert.ok(task.includes("git restore --source=HEAD -- src/api"), task);
	assert.ok(task.includes("run it again with a short timeout first"), task);
	// The retake carries new bindings with the try-2 run key.
	assert.deepEqual(retake.params.extensionBindings, {
		"sliceme/1": {
			campaign: BRANCH_KEY,
			node: "n0",
			run: "node-n0-try-2",
			dir: path.dirname(path.dirname(PLAN)) + "/.sliceme",
		},
	});
	// The retake's task keeps the node instruction, too.
	assert.ok(task.includes(`Plan file: ${PLAN}`));
}

// A second failure returns state blocked for that node only: the loop records
// the phase and reports the blocked node with its try count.
{
	const { runs, calls } = fakeRuns(
		{
			"ready-0": hostOk("ready-0", readyReply()),
			"ready-1": hostOk(
				"ready-1",
				JSON.stringify({ campaign: "feat.x", ready: [], nodes: [], dag: PLAN, worktree: WORKTREE, phase: 1, paused: false }),
			),
			"record-0": hostOk("record-0", "recorded"),
			"check-0": hostOk("check-0", "[passed] ok: true"),
			status: hostOk("status", JSON.stringify({ campaign: "feat.x" })),
		},
		{
			"node-n0": new Error("the run timed out and was terminated"),
			"node-n0-try-2": new Error("the run timed out and was terminated"),
		},
	);
	const result = await builtScript({ campaign: "feat.x" })(runs);
	assert.deepEqual(result.blocked, [
		{
			node: "n0",
			reason: "the run timed out and was terminated",
			tries: 2,
		},
	]);
	assert.equal(calls.run.filter((call) => call.key === "node-n0-try-2").length, 1);
	const record = calls.host.find((call) => call.key === "record-0");
	assert.ok(record, "the loop still records the phase");
}

// A child that returns status "blocked" is a failure for the retry: the
// salvage retake distinguishes a silent no-op child from a good child.
{
	const { runs, calls } = fakeRuns(
		{
			"ready-0": hostOk("ready-0", readyReply()),
			"ready-1": hostOk(
				"ready-1",
				JSON.stringify({ campaign: "feat.x", ready: [], nodes: [], dag: PLAN, worktree: WORKTREE, phase: 1, paused: false }),
			),
			"record-0": hostOk("record-0", "recorded"),
			"check-0": hostOk("check-0", "[passed] ok: true"),
			status: hostOk("status", JSON.stringify({ campaign: "feat.x" })),
		},
		{ "node-n0": "blocked" },
	);
	await builtScript({ campaign: "feat.x" })(runs);
	const retake = calls.run.find((call) => call.key === "node-n0-try-2");
	assert.ok(retake, "a blocked child relaunches once");
	assert.ok(
		retake.params.task.includes("Your first run ended with: the child ended with status blocked"),
		retake.params.task,
	);
}

// A node that declares no directory gets no reset directive: its salvage
// prompt orders no `git restore`, because a reset at the worktree root would
// destroy every sibling's work.
{
	const { runs, calls } = fakeRuns(
		{
			"ready-0": hostOk(
				"ready-0",
				readyReply({
					nodes: [{ id: "n0" }],
				}),
			),
			"ready-1": hostOk(
				"ready-1",
				JSON.stringify({ campaign: "feat.x", ready: [], nodes: [], dag: PLAN, worktree: WORKTREE, phase: 1, paused: false }),
			),
			"record-0": hostOk("record-0", "recorded"),
			"check-0": hostOk("check-0", "[passed] ok: true"),
			status: hostOk("status", JSON.stringify({ campaign: "feat.x" })),
		},
		{ "node-n0": new Error("the run failed") },
	);
	const result = await builtScript({ campaign: "feat.x" })(runs);
	assert.deepEqual(result.blocked, []);
	const retake = calls.run.find((call) => call.key === "node-n0-try-2");
	assert.ok(retake, "the no-directory child still relaunches once");
	assert.ok(retake.params.task.includes("Owned directory: (none)"), retake.params.task);
	assert.ok(
		!retake.params.task.includes("git restore"),
		`the no-directory retake carries no reset directive: ${retake.params.task}`,
	);
	assert.ok(
		retake.params.task.includes("read your own bounded progress tail: /root/.sliceme/feat--x.progress/n0.jsonl"),
		retake.params.task,
	);
}

// A noisy terminal reason stays inside the plan's evidence budget for the
// salvage prompt.
{
	const noisy = "n".repeat(EVIDENCE_LIMIT + 900);
	const { runs, calls } = fakeRuns(
		{
			"ready-0": hostOk("ready-0", readyReply()),
			"ready-1": hostOk(
				"ready-1",
				JSON.stringify({ campaign: "feat.x", ready: [], nodes: [], dag: PLAN, worktree: WORKTREE, phase: 1, paused: false }),
			),
			"record-0": hostOk("record-0", "recorded"),
			"check-0": hostOk("check-0", "[passed] ok: true"),
			status: hostOk("status", JSON.stringify({ campaign: "feat.x" })),
		},
		{ "node-n0": new Error(noisy) },
	);
	await builtScript({ campaign: "feat.x" })(runs);
	const retake = calls.run.find((call) => call.key === "node-n0-try-2");
	assert.ok(retake, "the failed child relaunches with a salvage prompt");
	assert.ok(
		retake.params.task.includes(`Your first run ended with: ${"n".repeat(EVIDENCE_LIMIT)}`),
		retake.params.task,
	);
	assert.ok(
		!retake.params.task.includes("n".repeat(EVIDENCE_LIMIT + 1)),
		"the salvage prompt caps the terminal reason",
	);
}

// A child that returned status "done" needs no retake.
{
	const { runs, calls } = fakeRuns({
		"ready-0": hostOk("ready-0", readyReply()),
		"ready-1": hostOk(
			"ready-1",
			JSON.stringify({ campaign: "feat.x", ready: [], nodes: [], dag: PLAN, worktree: WORKTREE, phase: 1, paused: false }),
		),
		"record-0": hostOk("record-0", "recorded"),
		"check-0": hostOk("check-0", "[passed] ok: true"),
		status: hostOk("status", JSON.stringify({ campaign: "feat.x" })),
	});
	const result = await builtScript({ campaign: "feat.x" })(runs);
	assert.equal(result.state, "complete");
	assert.deepEqual(result.blocked, []);
}

// Evidence larger than the reviewer budget, or cut by the host, arrives with
// both counts and the capture path, and the inline text stays bounded.
{
	const long = "y".repeat(EVIDENCE_LIMIT + 500);
	const cut = `[failed] noisy: python3 -c "..."\n${"z".repeat(64)}${HOST_CUT_MARKER}`;
	const { runs, calls } = fakeRuns({
		"ready-0": hostOk(
			"ready-0",
			readyReply({
				ready: ["n0", "n1"],
				nodes: [
					{ id: "n0", dir: "src/api" },
					{ id: "n1", dir: "src/core" },
				],
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
		"ready-0": hostOk("ready-0", readyReply({ ready: [], nodes: [] })),
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
			readyReply({
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
	assert.equal(calls.run.length, 0);
}

// The spend guard: when the engine reports the campaign spend is at its cap,
// the loop takes no try 2 at all and blocks every failed node.
{
	const { runs, calls } = fakeRuns(
		{
			"ready-0": hostOk(
				"ready-0",
				readyReply({ spend: { active_seconds: 3600, spent_usd: 9, retry: false } }),
			),
			"ready-1": hostOk(
				"ready-1",
				JSON.stringify({ campaign: "feat.x", ready: [], nodes: [], dag: PLAN, worktree: WORKTREE, phase: 1, paused: false }),
			),
			"record-0": hostOk("record-0", "recorded"),
			"check-0": hostOk("check-0", "[passed] ok: true"),
			status: hostOk("status", JSON.stringify({ campaign: "feat.x" })),
		},
		{ "node-n0": new Error("the run timed out and was terminated") },
	);
	const result = await builtScript({ campaign: "feat.x" })(runs);
	assert.equal(result.state, "blocked");
	assert.deepEqual(result.blocked, [
		{ node: "n0", reason: "the campaign spend is at its cap", tries: 1 },
	]);
	assert.equal(calls.run.find((call) => call.key === "node-n0-try-2"), undefined);
}
