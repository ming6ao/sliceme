/**
 * Unit tests for the `sliceme.plan` workflow resource in
 * `integrations/pi/plan-resource.ts`.
 *
 * The resolver is pure, so this harness pins the fixed host commands, the
 * re-pass bound, and the bounded-field validation without a pi runtime.
 * `tests/test_pi_package.py` runs it through Node's type stripping.
 */
import assert from "node:assert/strict";
import {
	PLAN_MAX_PASSES,
	PLAN_RESOURCE,
	PLAN_RESOURCE_FIELDS,
	planCommands,
	planGrants,
	resolvePlanResource,
} from "../integrations/pi/plan-resource.ts";

const ENGINE = ["python3", "/opt/sliceme/bin/sliceme"];

assert.equal(PLAN_RESOURCE, "sliceme.plan");
assert.deepEqual([...PLAN_RESOURCE_FIELDS].sort(), ["campaign", "design"]);
assert.equal(PLAN_MAX_PASSES, 3);

// The two fixed host commands, built from literals plus the engine path.
assert.deepEqual(planCommands(ENGINE), {
	status: "python3 /opt/sliceme/bin/sliceme --json status",
	ready: "python3 /opt/sliceme/bin/sliceme --json ready",
});
assert.equal(
	planCommands(ENGINE, "feat.x").ready,
	"python3 /opt/sliceme/bin/sliceme --json ready --campaign feat.x",
);

// One status grant and one ready grant per bounded pass.
assert.deepEqual(
	planGrants(ENGINE, undefined).map((grant) => grant.key),
	["status", "ready-0", "ready-1", "ready-2"],
);

// A full resolution exposes the grants and a script that uses them.
const resolved = resolvePlanResource({ design: "docs/PLAN.md" }, ENGINE);
assert.ok(!("error" in resolved), JSON.stringify(resolved));
assert.deepEqual(
	resolved.hostCommands.map((grant) => grant.key),
	["status", "ready-0", "ready-1", "ready-2"],
);
assert.ok(resolved.script.includes("const MAX_PASSES = 3;"));
assert.ok(resolved.script.includes('const DESIGN = "docs/PLAN.md";'));
assert.ok(resolved.script.includes('"sliceme-planner"'));
assert.ok(resolved.script.includes('"planner-" + pass'));
assert.ok(resolved.script.includes('"ready-" + pass'));
assert.ok(resolved.script.includes('state: "planned"'));
assert.ok(resolved.script.includes('state: "failed"'));
assert.ok(resolved.script.includes("The engine rejected the previous plan"));
for (const grant of resolved.hostCommands) {
	assert.ok(
		resolved.script.includes(JSON.stringify(grant.command)),
		`the script does not call the granted ${grant.key} command`,
	);
}

// The plan resource never records, checks, approves, opens, or delivers.
for (const gone of ["record", "check", "approve", "deliver", "open"]) {
	assert.ok(!resolved.hostCommands.some((grant) => grant.key === gone), gone);
	assert.ok(!resolved.script.includes(`"${gone}"`), gone);
}

// The design path defaults and is embedded as a literal.
const defaulted = resolvePlanResource({}, ENGINE);
assert.ok(!("error" in defaulted));
assert.ok(defaulted.script.includes('const DESIGN = "DESIGN.md";'));

// Unknown fields are rejected.
const unknown = resolvePlanResource({ task: "plan this" }, ENGINE);
assert.ok("error" in unknown && unknown.error.includes("unsupported fields: task"));

// The campaign token is strict.
for (const campaign of ["feat/x", "", "a b", "x".repeat(129)]) {
	assert.ok("error" in resolvePlanResource({ campaign }, ENGINE), String(campaign));
}

// The design path rejects a control character and an empty value.
for (const design of ["", "   ", "a\nb", "a\rb"]) {
	assert.ok("error" in resolvePlanResource({ design }, ENGINE), JSON.stringify(design));
}

// A missing engine path fails closed.
assert.ok("error" in resolvePlanResource({}, []));
