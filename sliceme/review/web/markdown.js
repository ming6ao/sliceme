// Safe Markdown rendering for the Sliceme review client.
//
// The renderer builds DOM nodes directly. It never assigns `innerHTML`, so a
// report can never inject HTML or script. `parseMarkdown` and `parseInline`
// are pure functions that return token trees. A Node harness tests them.

const BLANK = /^\s*$/;
const FENCE = /^\s{0,3}(`{3,}|~{3,})\s*([^`]*?)\s*$/;
const FENCE_CLOSE = /^\s{0,3}(`{3,}|~{3,})\s*$/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const HR = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const SEPARATOR = /^:?-+:?$/;
const WORD = /[A-Za-z0-9]/;
const LEADING_SPACE = /^\s*/;

const INLINE_SOURCE = [
	"(?<code>`+)(?<codeBody>[\\s\\S]*?)\\k<code>",
	"!\\[(?<imageAlt>[^\\]]*)\\]\\([^)]*\\)",
	"\\[(?<linkText>[^\\]]*)\\]\\((?<linkUrl>[^)]*)\\)",
	"<(?<auto>(?:https?:\\/\\/|mailto:)[^>\\s]+)>",
	"\\*\\*(?<strong>[\\s\\S]+?)\\*\\*|__(?<strong2>[\\s\\S]+?)__",
	"~~(?<del>[\\s\\S]+?)~~",
	"\\*(?<em>[^*\\n]+?)\\*|_(?<em2>[^_\\n]+?)_",
].join("|");

/** Allow only `http`, `https`, `mailto`, and relative or fragment links. */
export function safeUrl(url) {
	const value = String(url ?? "").trim();
	if (!value || value.startsWith("//")) return null;
	if (/^(https?:|mailto:)/i.test(value)) return value;
	if (/^[a-z][a-z0-9+.-]*:/i.test(value)) return null;
	return value;
}

/** Parse a full Markdown document into block tokens. */
export function parseMarkdown(text) {
	const lines = String(text ?? "")
		.replace(/\r\n?/g, "\n")
		.split("\n");
	return blocks(lines);
}

function blocks(lines) {
	const out = [];
	for (let i = 0; i < lines.length; ) {
		const found = blockAt(lines, i);
		if (found) {
			out.push(found.block);
			i = found.next;
			continue;
		}
		if (BLANK.test(lines[i])) {
			i += 1;
			continue;
		}
		const paragraph = [lines[i]];
		for (i += 1; i < lines.length && !BLANK.test(lines[i]) && !blockAt(lines, i); i += 1) {
			paragraph.push(lines[i]);
		}
		out.push({ type: "paragraph", text: paragraph.join("\n") });
	}
	return out;
}

function blockAt(lines, index) {
	const line = lines[index];
	let match;
	if ((match = line.match(FENCE))) return fencedBlock(lines, index, match);
	if ((match = line.match(HEADING))) {
		return { block: { type: "heading", level: match[1].length, text: match[2] }, next: index + 1 };
	}
	if (HR.test(line)) return { block: { type: "hr" }, next: index + 1 };
	if (QUOTE.test(line)) return quotedBlock(lines, index);
	if (ITEM.test(line)) return listBlock(lines, index);
	if (line.includes("|") && isSeparator(lines[index + 1])) return tableBlock(lines, index);
	return null;
}

function fencedBlock(lines, index, match) {
	const marker = match[1][0];
	const min = match[1].length;
	const body = [];
	let i = index + 1;
	for (; i < lines.length; i += 1) {
		const close = lines[i].match(FENCE_CLOSE);
		if (close && close[1][0] === marker && close[1].length >= min) {
			i += 1;
			break;
		}
		body.push(lines[i]);
	}
	return { block: { type: "code", lang: match[2].trim(), text: body.join("\n") }, next: i };
}

function quotedBlock(lines, index) {
	const inner = [];
	let i = index;
	for (; i < lines.length && QUOTE.test(lines[i]); i += 1) inner.push(lines[i].match(QUOTE)[1]);
	return { block: { type: "blockquote", blocks: blocks(inner) }, next: i };
}

function tableBlock(lines, index) {
	const header = cells(lines[index]);
	const rows = [];
	let i = index + 2;
	for (; i < lines.length && !BLANK.test(lines[i]) && lines[i].includes("|"); i += 1) {
		rows.push(cells(lines[i]));
	}
	return { block: { type: "table", header, rows }, next: i };
}

function listBlock(lines, start) {
	const first = lines[start].match(ITEM);
	const indent = first[1].length;
	const ordered = /^\d/.test(first[2]);
	const width = indent + first[2].length + 1;
	const items = [];
	let i = start;
	while (i < lines.length) {
		const marker = lines[i].match(ITEM);
		if (!marker || marker[1].length !== indent || /^\d/.test(marker[2]) !== ordered) break;
		const item = [marker[3]];
		for (i += 1; i < lines.length; i += 1) {
			const line = lines[i];
			const next = line.match(ITEM);
			if (next && next[1].length === indent && /^\d/.test(next[2]) === ordered) break;
			if (BLANK.test(line) || line.match(LEADING_SPACE)[0].length <= indent) break;
			item.push(line);
		}
		items.push({ blocks: blocks(strip(item, width)) });
	}
	const number = ordered ? parseInt(first[2], 10) : 1;
	return { block: { type: "list", ordered, start: number, items }, next: i };
}

function cells(line) {
	let text = line.trim();
	if (text.startsWith("|")) text = text.slice(1);
	if (text.endsWith("|")) text = text.slice(0, -1);
	return text.split("|").map((cell) => cell.trim());
}

function isSeparator(line) {
	if (!line) return false;
	const row = cells(line);
	return row.length > 0 && row.every((cell) => SEPARATOR.test(cell));
}

function strip(lines, width) {
	return lines.map((line) => {
		let count = 0;
		while (count < width && line[count] === " ") count += 1;
		return line.slice(count);
	});
}

/** Parse inline Markdown into tokens. Each call scans with its own regex. */
export function parseInline(text) {
	const source = String(text ?? "");
	const pattern = new RegExp(INLINE_SOURCE, "g");
	const tokens = [];
	let last = 0;
	let match;
	while ((match = pattern.exec(source)) !== null) {
		if (match.index > last) tokens.push({ type: "text", value: source.slice(last, match.index) });
		const group = match.groups;
		if (group.code !== undefined) {
			tokens.push({ type: "code", value: group.codeBody.replace(/\n/g, " ") });
		} else if (group.imageAlt !== undefined) {
			tokens.push({ type: "text", value: group.imageAlt });
		} else if (group.linkText !== undefined) {
			const href = safeUrl(group.linkUrl);
			if (href) tokens.push({ type: "link", href, children: parseInline(group.linkText || href) });
			else tokens.push({ type: "text", value: group.linkText });
		} else if (group.auto !== undefined) {
			tokens.push({ type: "link", href: group.auto, children: [{ type: "text", value: group.auto }] });
		} else if (group.strong !== undefined || group.strong2 !== undefined) {
			tokens.push({ type: "strong", children: parseInline(group.strong ?? group.strong2) });
		} else if (group.del !== undefined) {
			tokens.push({ type: "del", children: parseInline(group.del) });
		} else if (group.em !== undefined) {
			tokens.push({ type: "em", children: parseInline(group.em) });
		} else if (group.em2 !== undefined) {
			const before = match.index > 0 ? source[match.index - 1] : " ";
			const after = pattern.lastIndex < source.length ? source[pattern.lastIndex] : " ";
			if (WORD.test(before) && WORD.test(after)) tokens.push({ type: "text", value: match[0] });
			else tokens.push({ type: "em", children: parseInline(group.em2) });
		}
		last = pattern.lastIndex;
	}
	if (last < source.length) tokens.push({ type: "text", value: source.slice(last) });
	return tokens;
}

/** Render Markdown text into `container` with DOM nodes only. */
export function renderMarkdown(container, text) {
	container.textContent = "";
	for (const block of parseMarkdown(text)) container.append(renderBlock(block));
	return container;
}

function renderBlock(block) {
	if (block.type === "heading") {
		const el = document.createElement(`h${block.level}`);
		appendInline(el, parseInline(block.text));
		return el;
	}
	if (block.type === "paragraph") {
		const el = document.createElement("p");
		appendInline(el, parseInline(block.text.replace(/\n/g, " ")));
		return el;
	}
	if (block.type === "code") {
		const pre = document.createElement("pre");
		const code = document.createElement("code");
		if (block.lang) code.className = `language-${block.lang}`;
		code.textContent = block.text;
		pre.append(code);
		return pre;
	}
	if (block.type === "hr") return document.createElement("hr");
	if (block.type === "blockquote") {
		const el = document.createElement("blockquote");
		for (const inner of block.blocks) el.append(renderBlock(inner));
		return el;
	}
	if (block.type === "list") {
		const el = document.createElement(block.ordered ? "ol" : "ul");
		if (block.ordered && block.start !== 1) el.start = block.start;
		for (const item of block.items) {
			const li = document.createElement("li");
			for (const inner of item.blocks) li.append(renderBlock(inner));
			el.append(li);
		}
		return el;
	}
	if (block.type === "table") {
		const table = document.createElement("table");
		const head = document.createElement("thead");
		const headRow = document.createElement("tr");
		for (const cell of block.header) {
			const th = document.createElement("th");
			appendInline(th, parseInline(cell));
			headRow.append(th);
		}
		head.append(headRow);
		table.append(head);
		const body = document.createElement("tbody");
		for (const row of block.rows) {
			const tr = document.createElement("tr");
			for (const cell of row) {
				const td = document.createElement("td");
				appendInline(td, parseInline(cell));
				tr.append(td);
			}
			body.append(tr);
		}
		table.append(body);
		return table;
	}
	return document.createTextNode("");
}

function appendInline(el, tokens) {
	for (const token of tokens) {
		if (token.type === "text") {
			el.append(document.createTextNode(token.value));
		} else if (token.type === "code") {
			const code = document.createElement("code");
			code.textContent = token.value;
			el.append(code);
		} else if (token.type === "link") {
			const link = document.createElement("a");
			link.href = token.href;
			link.rel = "noopener noreferrer";
			link.target = "_blank";
			appendInline(link, token.children);
			el.append(link);
		} else {
			const tag = { strong: "strong", em: "em", del: "del" }[token.type];
			const child = document.createElement(tag);
			appendInline(child, token.children);
			el.append(child);
		}
	}
}
