/**
 * Unit tests for the active-campaign pointer file in `common.ts`.
 *
 * The pointer binds a coordinator session to one campaign when several
 * campaigns share a plane. It is the primary source for `activeCampaign`; the
 * engine's `config.json` mirror is the fallback.
 */
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
	activeCampaignPath,
	clearActiveCampaign,
	readActiveCampaign,
	writeActiveCampaign,
} from "../integrations/pi/common.ts";

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "sliceme-active-"));

// No pointer yet.
assert.equal(readActiveCampaign(dir, 111), undefined);

writeActiveCampaign(dir, "feat/x", 111);
assert.equal(readActiveCampaign(dir, 111), "feat/x");

// A different process id has its own pointer.
writeActiveCampaign(dir, "feat/y", 222);
assert.equal(readActiveCampaign(dir, 111), "feat/x");
assert.equal(readActiveCampaign(dir, 222), "feat/y");

// The path carries the process id and the state directory.
const expected = path.join(dir, ".sliceme", "active.111.campaign");
assert.equal(activeCampaignPath(dir, 111), expected);
assert.ok(fs.existsSync(expected));

// Clearing one pointer leaves the other.
clearActiveCampaign(dir, 111);
assert.equal(readActiveCampaign(dir, 111), undefined);
assert.equal(readActiveCampaign(dir, 222), "feat/y");
clearActiveCampaign(dir, 111); // idempotent

fs.rmSync(dir, { recursive: true, force: true });
console.log("active_campaign_test: OK");
