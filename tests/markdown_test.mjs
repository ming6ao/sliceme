/**
 * Unit tests for the safe Markdown parser in `sliceme/review/web/markdown.js`.
 *
 * The module is a browser ES module, so this harness imports its source through
 * a data URL. The pure `parseMarkdown`, `parseInline`, and `safeUrl` functions
 * need no DOM. The tests also pin the security invariant: the renderer never
 * builds HTML from report text.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const sourcePath = path.join(here, "..", "sliceme", "review", "web", "markdown.js");
const source = readFileSync(sourcePath, "utf8");
const module = await import(
	"data:text/javascript;base64," + Buffer.from(source).toString("base64")
);
const { parseMarkdown, parseInline, safeUrl } = module;

// Block parsing.
const blocks = parseMarkdown(
	[
		"# Title",
		"",
		"Intro with **bold** text.",
		"",
		"```python",
		"x = 1",
		"```",
		"",
		"- one",
		"- two",
		"  - nested",
		"",
		"1. first",
		"2. second",
		"",
		"> quoted line",
		"",
		"| a | b |",
		"| --- | ---: |",
		"| 1 | 2 |",
		"",
		"---",
	].join("\n"),
);
assert.deepEqual(
	blocks.map((block) => block.type),
	["heading", "paragraph", "code", "list", "list", "blockquote", "table", "hr"],
);
assert.equal(blocks[0].level, 1);
assert.equal(blocks[2].lang, "python");
assert.equal(blocks[2].text, "x = 1");
assert.equal(blocks[3].ordered, false);
assert.ok(blocks[3].items[1].blocks.some((block) => block.type === "list"));
assert.equal(blocks[4].ordered, true);
assert.equal(blocks[4].start, 1);
assert.deepEqual(blocks[6].header, ["a", "b"]);
assert.deepEqual(blocks[6].rows, [["1", "2"]]);

// Inline parsing.
const inline = parseInline("a **b** _c_ `d` [e](https://x) ~~f~~");
assert.deepEqual(
	inline.map((token) => token.type),
	["text", "strong", "text", "em", "text", "code", "text", "link", "text", "del"],
);
assert.equal(inline[7].href, "https://x");
// An intraword underscore must stay text.
const snake = parseInline("snake_case");
assert.deepEqual(snake, [{ type: "text", value: "snake_case" }]);

// URL safety.
assert.equal(safeUrl("https://example.com/a"), "https://example.com/a");
assert.equal(safeUrl("mailto:a@b.c"), "mailto:a@b.c");
assert.equal(safeUrl("#anchor"), "#anchor");
assert.equal(safeUrl("javascript:alert(1)"), null);
assert.equal(safeUrl("data:text/html,x"), null);
assert.equal(safeUrl("//evil.example"), null);

// A raw HTML tag in the report stays literal text.
const html = parseInline("<img src=x onerror=alert(1)>");
assert.ok(html.every((token) => token.type === "text"));

// Security invariant: the module never builds HTML from report text.
assert.ok(!/\.(inner|outer)HTML\s*=/.test(source), "markdown.js must not assign innerHTML");
assert.ok(!/insertAdjacentHTML\s*\(/.test(source), "markdown.js must not insert HTML");

console.log("markdown_test: OK");
