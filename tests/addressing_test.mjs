/**
 * Unit tests for the addressing helpers in `common.ts`.
 *
 * `addressingSessionId` derives the one persistent pi session per campaign, and
 * `addressingBatches` groups resolved comments into one addressing pass per
 * node. Both are pure, so this harness pins them down without a pi runtime.
 * `tests/test_pi_package.py` runs it through Node's type stripping.
 */
import assert from "node:assert/strict";
import { addressingBatches, addressingSessionId } from "../integrations/pi/common.ts";

// A stable, pi-valid session id per campaign: letters/digits/dot/underscore/dash,
// starting and ending with a letter or digit.
const ID_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$/;

assert.equal(addressingSessionId("main"), "sliceme-addressing-main");
assert.equal(addressingSessionId("feat/x"), "sliceme-addressing-feat--x");
assert.equal(addressingSessionId("feat/x"), addressingSessionId("feat/x"));
assert.equal(addressingSessionId(""), "sliceme-addressing-main");

// A branch name that is not a valid id is sanitized into one.
const messy = addressingSessionId("feat/weird name!");
assert.equal(messy, "sliceme-addressing-feat--weird-name");
for (const branch of ["main", "feat/x", "feat/weird name!", "release.2024", "_leading"]) {
	const id = addressingSessionId(branch);
	assert.match(id, ID_PATTERN, id);
	assert.ok(id.length <= 80, id);
}

assert.deepEqual(
	addressingBatches(
		[
			{ id: 1 },
			{ id: 2 },
			{ id: 3 },
			{ id: 4 },
		],
		[
			{ comment: 1, node: "w1", reason: "owns" },
			{ comment: 2, node: "w1", reason: "explicit" },
			{ comment: 3, node: null, reason: "general" },
			{ comment: 4, node: "w2", reason: "owns" },
		],
	),
	[
		{ node: "w1", comments: [{ id: 1 }, { id: 2 }] },
		{ node: null, comments: [{ id: 3 }] },
		{ node: "w2", comments: [{ id: 4 }] },
	],
);

// A comment with no resolve is reply-only, and each null-node comment is its own turn.
assert.deepEqual(
	addressingBatches([{ id: 7 }, { id: 8 }], [{ comment: 8, node: null, reason: "outside_owns" }]),
	[
		{ node: null, comments: [{ id: 7 }] },
		{ node: null, comments: [{ id: 8 }] },
	],
);

// `comment` may arrive as a string from the JSON poll; it still groups.
assert.deepEqual(
	addressingBatches(
		[{ id: 5 }, { id: 6 }],
		[
			{ comment: 5, node: "w3", reason: "owns" },
			{ comment: 6, node: "w3", reason: "owns" },
		],
	),
	[{ node: "w3", comments: [{ id: 5 }, { id: 6 }] }],
);

console.log("addressing helpers OK");
