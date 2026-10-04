/**
 * Unit tests for `CampaignStateStore` in `common.ts`.
 *
 * The store is the single in-process writer of a campaign's `state.json`. All
 * callers share one state object, so two parallel `spawn` completions cannot
 * drop each other's node status.
 * `tests/test_pi_package.py` runs this harness through Node's type stripping.
 */
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { CampaignStateStore, readJson, writeJson } from "../integrations/pi/common.ts";

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "sliceme-store-"));
const file = path.join(dir, "state.json");
writeJson(file, { nodes: { a: { status: "pending" } } });

const store = new CampaignStateStore(file);

// Two completions read the same object, so both mutations survive one flush.
const first = store.read();
const second = store.read();
assert.equal(first, second, "the store hands out one shared state object");
first.nodes.a = { status: "done" };
second.nodes.b = { status: "failed" };
store.save();
let onDisk = readJson(file, {});
assert.equal(onDisk.nodes.a.status, "done");
assert.equal(onDisk.nodes.b.status, "failed");

// `replace` plus `save` persists a whole new state.
store.replace({ nodes: { c: { status: "done" } } });
store.save();
onDisk = readJson(file, {});
assert.equal(onDisk.nodes.c.status, "done");
assert.equal(onDisk.nodes.a, undefined);

// `save(false)` leaves the file alone, for delivery with `cleanup all`.
store.replace({ nodes: { d: { status: "done" } } });
store.save(false);
onDisk = readJson(file, {});
assert.equal(onDisk.nodes.c.status, "done");
assert.equal(onDisk.nodes.d, undefined);

// The atomic writer leaves no temporary file behind.
writeJson(file, { nodes: {}, x: 1 });
assert.equal(readJson(file, {}).x, 1);
assert.ok(
	!fs.readdirSync(dir).some((name) => name.endsWith(".tmp")),
	"no temporary file remains",
);

// `reload` reads the file again.
writeJson(file, { nodes: { e: {} } });
store.reload();
assert.ok(store.read().nodes.e);

fs.rmSync(dir, { recursive: true, force: true });
console.log("state_store_test: ok");
