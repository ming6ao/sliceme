// Diff rendering and line anchoring for the Sliceme review client.
//
// All plane text is written with `textContent`, never HTML, so a commit message,
// a file name, or a comment body can never become markup.

/** The comment anchor for one diff line, or `null` for a header row. */
export function anchorFor(line) {
	if (!line || line.type === "hunk" || line.type === "meta") return null;
	if (line.type === "delete") return { side: "old", line: line.old };
	return { side: "new", line: line.new };
}

/**
 * Render parsed diff lines into `container`.
 *
 * Handlers:
 * - `onSelect(anchor, { extend })` when a line is clicked;
 * - `onComment(anchor)` when the gutter `+` or the `c` key opens a comment.
 */
export function renderDiff(container, lines, handlers = {}) {
	container.textContent = "";
	const rows = [];
	for (const line of lines) {
		const row = document.createElement("div");
		row.className = `diff-line ${line.type}`;

		const oldNo = document.createElement("span");
		oldNo.className = "no old";
		oldNo.textContent = line.old === null || line.old === undefined ? "" : String(line.old);

		const newNo = document.createElement("span");
		newNo.className = "no new";
		newNo.textContent = line.new === null || line.new === undefined ? "" : String(line.new);

		const gutter = document.createElement("button");
		gutter.type = "button";
		gutter.className = "gutter";
		gutter.textContent = "+";
		gutter.setAttribute("aria-label", "Comment on this line");

		const text = document.createElement("span");
		text.className = "text";
		text.textContent = line.text;

		row.append(oldNo, newNo, gutter, text);

		const anchor = anchorFor(line);
		if (anchor) {
			row.tabIndex = 0;
			row.dataset.side = anchor.side;
			row.dataset.line = String(anchor.line);
			row.addEventListener("click", (event) => {
				handlers.onSelect?.(anchor, { extend: event.shiftKey });
			});
			gutter.addEventListener("click", (event) => {
				event.stopPropagation();
				handlers.onSelect?.(anchor, { extend: false });
				handlers.onComment?.(anchor);
			});
			row.addEventListener("keydown", (event) => {
				if (event.key === "c") {
					event.preventDefault();
					handlers.onSelect?.(anchor, { extend: false });
					handlers.onComment?.(anchor);
				}
			});
		}
		container.append(row);
		rows.push(row);
	}
	return rows;
}

/** Mark the anchor rows and clear any earlier mark. */
export function markAnchor(container, anchor, range) {
	for (const row of container.querySelectorAll(".diff-line.anchored")) {
		row.classList.remove("anchored");
	}
	if (!anchor) return;
	const first = Math.min(anchor.line, range?.line ?? anchor.line);
	const last = Math.max(anchor.line, range?.line ?? anchor.line);
	for (const row of container.querySelectorAll(".diff-line")) {
		const side = row.dataset.side;
		const line = Number(row.dataset.line);
		if (!side || !line) continue;
		if (line >= first && line <= last) row.classList.add("anchored");
	}
}
