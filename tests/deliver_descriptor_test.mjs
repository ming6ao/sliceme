/**
 * Unit tests for `applyEngineReply` in `integrations/pi/common.ts`.
 *
 * The engine's `deliver` reply carries `target_branch`, not `feature_branch`.
 * The reply handler must still mark the session descriptor `completed`, and it
 * must do so without an active-campaign pointer, so a successful delivery never
 * leaves a `suspended` descriptor (`docs/sessions.md` §6).
 *
 * `tests/test_pi_package.py` runs this harness through Node's type stripping.
 */
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
	applyEngineReply,
	readActiveCampaign,
	readJson,
	sessionPath,
} from "../integrations/pi/common.ts";

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "sliceme-deliver-"));

/** Write a suspended descriptor for one campaign branch. */
function writeDescriptor(branch) {
	fs.mkdirSync(path.join(dir, ".sliceme"), { recursive: true });
	fs.writeFileSync(
		sessionPath(dir, branch),
		JSON.stringify({ campaign: "checkout", feature_branch: branch, status: "suspended" }),
	);
}

// A deliver reply names `target_branch`.  There is no active-campaign pointer,
// yet the descriptor must flip to `completed`.
writeDescriptor("feat/checkout");
const deliver = {
	target_branch: "feat/checkout",
	source: "sliceme/feat-checkout",
	results: [{ status: "landed" }],
};
const branch = applyEngineReply(dir, "deliver", deliver);
assert.equal(branch, "feat/checkout");
assert.equal(readActiveCampaign(dir), "feat/checkout");
assert.equal(readJson(sessionPath(dir, "feat/checkout"), {}).status, "completed");
assert.ok(readJson(sessionPath(dir, "feat/checkout"), {}).completed_at);

// A deliver reply that did not land leaves the descriptor untouched.
writeDescriptor("feat/other");
applyEngineReply(dir, "deliver", {
	target_branch: "feat/other",
	results: [{ status: "failed" }],
});
assert.equal(readJson(sessionPath(dir, "feat/other"), {}).status, "suspended");

// A non-deliver reply still sets the pointer but never completes the
// descriptor.
writeDescriptor("feat/third");
applyEngineReply(dir, "status", { target_branch: "feat/third" });
assert.equal(readActiveCampaign(dir), "feat/third");
assert.equal(readJson(sessionPath(dir, "feat/third"), {}).status, "suspended");

fs.rmSync(dir, { recursive: true, force: true });
console.log("deliver_descriptor_test: OK");
