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
assert.deepEqual([...CAMPAIGN_RESOURCE_FIELDS].sort(), ["campaign", "nodeCap", "waveCap"]);
assert.ok(CAMPAIGN_TOKEN.test("feat.x"));
assert.ok(!CAMPAIGN_TOKEN.test("feat/x"));
assert.ok(!CAMPAIGN_TOKEN.test(""));

// The six fixed host commands, built from literals plus the engine path.
assert.deepEqual(campaignCommands(ENGINE), {
	status: "python3 /opt/sliceme/bin/sliceme --json status",
	ready: "python3 /opt/sliceme/bin/sliceme --json ready",
	record: "python3 /opt/sliceme/bin/sliceme --json wave --record --current",
	check: "python3 /opt/sliceme/bin/sliceme --json check --current",
	approve: "python3 /opt/sliceme/bin/sliceme --json review --decision approve",
	deliver: "python3 /opt/sliceme/bin/sliceme --json deliver",
});

// The campaign token is the only variable text and is bound with --campaign.
const withCampaign = campaignCommands(ENGINE, "feat.x");
assert.equal(
	withCampaign.status,
	"python3 /opt/sliceme/bin/sliceme --json status --campaign feat.x",
);
assert.equal(
	withCampaign.record,
	"python3 /opt/sliceme/bin/sliceme --json wave --record --current --campaign feat.x",
);

// A full resolution exposes the six grants and a script that uses the state
// commands.
const resolved = resolveCampaignResource({ campaign: "feat.x", waveCap: 3, nodeCap: 9 }, ENGINE);
assert.ok(!("error" in resolved), JSON.stringify(resolved));
assert.deepEqual(
	resolved.hostCommands.map((grant) => grant.key),
	["status", "ready", "record", "check", "approve", "deliver"],
);
for (const key of ["status", "ready", "record", "check"]) {
	const grant = resolved.hostCommands.find((entry) => entry.key === key);
	assert.ok(grant, `missing grant ${key}`);
	assert.ok(
		resolved.script.includes(JSON.stringify(grant.command)),
		`the script does not call the granted ${key} command`,
	);
}
assert.ok(resolved.script.includes('"sliceme-worker"'));
assert.ok(resolved.script.includes('"reviewer"'));
assert.ok(resolved.script.includes("const WAVE_CAP = 3;"));
assert.ok(resolved.script.includes("const NODE_CAP = 9;"));

// An omitted bound falls back to the bounded default (64 waves / 256 nodes).
const defaulted = resolveCampaignResource({}, ENGINE);
assert.ok(!("error" in defaulted));
assert.ok(defaulted.script.includes("const WAVE_CAP = 64;"));
assert.ok(defaulted.script.includes("const NODE_CAP = 256;"));

// Unknown fields are rejected.
const unknown = resolveCampaignResource({ task: "review this" }, ENGINE);
assert.ok("error" in unknown && unknown.error.includes("unsupported fields: task"));

// The campaign token is strict.
for (const campaign of ["feat/x", "", "a b", "x".repeat(129)]) {
	const bad = resolveCampaignResource({ campaign }, ENGINE);
	assert.ok("error" in bad, `accepted campaign ${JSON.stringify(campaign)}`);
}

// The caps are bounded.
for (const waveCap of [0, 65, 1.5, "3"]) {
	assert.ok("error" in resolveCampaignResource({ waveCap }, ENGINE), String(waveCap));
}
for (const nodeCap of [0, 257, 1.5, "3"]) {
	assert.ok("error" in resolveCampaignResource({ nodeCap }, ENGINE), String(nodeCap));
}

// A missing engine path fails closed.
assert.ok("error" in resolveCampaignResource({}, []));
