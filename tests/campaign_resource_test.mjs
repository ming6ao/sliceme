/**
 * Unit tests for the `sliceme.campaign` workflow resource in
 * `integrations/pi/campaign-resource.ts`.
 *
 * The resolver is pure, so this harness pins the fixed host commands, the
 * bounded-field validation, and the campaign-token rejection without a pi
 * runtime. `tests/test_pi_package.py` runs it through Node's type stripping.
 */
import assert from "node:assert/strict";
import {
	CAMPAIGN_RESOURCE,
	CAMPAIGN_RESOURCE_FIELDS,
	CAMPAIGN_TOKEN,
	campaignCommands,
	resolveCampaignResource,
} from "../integrations/pi/campaign-resource.ts";

const ENGINE = ["python3", "/opt/sliceme/bin/sliceme"];

assert.equal(CAMPAIGN_RESOURCE, "sliceme.campaign");
assert.deepEqual([...CAMPAIGN_RESOURCE_FIELDS].sort(), ["campaign", "nodeCap", "phaseCap"]);
assert.ok(CAMPAIGN_TOKEN.test("feat.x"));
assert.ok(!CAMPAIGN_TOKEN.test("feat/x"));
assert.ok(!CAMPAIGN_TOKEN.test(""));

// The four fixed host commands, built from literals plus the engine path.
assert.deepEqual(campaignCommands(ENGINE), {
	status: "python3 /opt/sliceme/bin/sliceme --json status",
	ready: "python3 /opt/sliceme/bin/sliceme --json ready",
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
// The worker starts in the campaign worktree. The reply carries the path and
// the node definition, because the workflow sandbox cannot read `.sliceme/`.
assert.ok(resolved.script.includes("ready.worktree"));
assert.ok(resolved.script.includes("ready.nodes"));
assert.ok(resolved.script.includes("cwd: worktree"));
assert.ok(resolved.script.includes("Worktree: "));
assert.ok(resolved.script.includes("Owned directory: "));
assert.ok(resolved.script.includes("Goal: "));
assert.ok(resolved.script.includes("Tasks:"));
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
