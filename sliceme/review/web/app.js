// Sliceme review client: per-commit approvals, the report, and the action calls.
//
// No build step and no framework. The write token arrives in the URL fragment
// (`#token=...`) and is sent as `X-Sliceme-Token` on every write. Selection
// updates use `history.replaceState`, so the token never enters history.

import { markAnchor, renderDiff } from "/diff.js";

const els = {
	targetName: document.getElementById("target-name"),
	tips: document.getElementById("tips"),
	approvalState: document.getElementById("approval-state"),
	stale: document.getElementById("stale"),
	approveAll: document.getElementById("approve-all"),
	deliver: document.getElementById("deliver"),
	commitList: document.getElementById("commit-list"),
	fileTree: document.getElementById("file-tree"),
	diffHeader: document.getElementById("diff-header"),
	diff: document.getElementById("diff"),
	report: document.getElementById("report"),
	evidence: document.getElementById("evidence"),
	comments: document.getElementById("comments"),
	commentCount: document.getElementById("comment-count"),
	commentForm: document.getElementById("comment-form"),
	commentAnchor: document.getElementById("comment-anchor"),
	commentBody: document.getElementById("comment-body"),
	commentCancel: document.getElementById("comment-cancel"),
	statusbar: document.getElementById("statusbar"),
	notice: document.getElementById("notice"),
};

const view = {
	token: "",
	plane: "",
	commit: "",
	file: "",
	report: false,
	snapshot: null,
	pinned: null,
	anchor: null,
	range: null,
	stale: false,
};

// ---------------------------------------------------------------------------
// Hash and API
// ---------------------------------------------------------------------------
function readHash() {
	const params = new URLSearchParams(location.hash.replace(/^#/, ""));
	if (params.get("token")) view.token = params.get("token");
	if (params.get("plane")) view.plane = params.get("plane");
	view.commit = params.get("commit") || "";
	view.file = params.get("file") || "";
	view.report = params.get("report") === "1";
}

function writeHash() {
	const params = new URLSearchParams();
	if (view.token) params.set("token", view.token);
	if (view.plane) params.set("plane", view.plane);
	if (view.commit) params.set("commit", view.commit);
	if (view.file) params.set("file", view.file);
	if (view.report) params.set("report", "1");
	history.replaceState(null, "", "#" + params.toString());
}

async function apiGet(path, params) {
	const query = new URLSearchParams();
	for (const [key, value] of Object.entries(params || {})) {
		if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
	}
	const suffix = query.toString() ? "?" + query.toString() : "";
	const response = await fetch(path + suffix);
	const body = await response.json().catch(() => ({}));
	if (!response.ok) throw new Error(body.error || response.statusText);
	return body;
}

async function apiPost(action, params) {
	const response = await fetch("/api/action", {
		method: "POST",
		headers: { "Content-Type": "application/json", "X-Sliceme-Token": view.token },
		body: JSON.stringify({ action, params: { plane: view.plane, ...params } }),
	});
	const body = await response.json().catch(() => ({}));
	if (!response.ok) throw new Error(body.error || response.statusText);
	return body;
}

function notice(message, isError = false) {
	els.notice.textContent = message;
	els.notice.style.background = isError ? "#b54708" : "";
	els.notice.hidden = false;
	clearTimeout(notice._timer);
	notice._timer = setTimeout(() => {
		els.notice.hidden = true;
	}, 4000);
}

function shortHash(hash) {
	return hash ? String(hash).slice(0, 7) : "none";
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function renderTopbar() {
	const snapshot = view.snapshot;
	if (!snapshot) return;
	els.targetName.textContent = `sliceme review · ${snapshot.feature_branch}`;
	els.tips.textContent = `${shortHash(snapshot.source_tip)} → ${shortHash(snapshot.target_tip)}`;
	const unapproved = (snapshot.commits || []).filter((commit) => !commit.approved).length;
	els.approvalState.textContent = snapshot.all_approved
		? "all commits approved"
		: `${unapproved} unapproved`;
	els.stale.hidden = !view.stale;
	els.approveAll.disabled = view.stale || unapproved === 0;
	els.deliver.hidden = !snapshot.all_approved || view.stale;
}

function renderCommits() {
	const snapshot = view.snapshot;
	if (!snapshot) return;
	els.commitList.textContent = "";
	for (const commit of snapshot.commits || []) {
		const row = document.createElement("div");
		row.className = "commit-row";
		if (commit.hash === view.commit) row.classList.add("selected");

		const toggle = document.createElement("button");
		toggle.type = "button";
		toggle.className = "approve-toggle";
		toggle.textContent = commit.approved ? "✓" : "○";
		toggle.title = commit.approved ? "Approved; click to request changes" : "Approve this commit";
		toggle.setAttribute("aria-pressed", String(commit.approved));
		toggle.addEventListener("click", (event) => {
			event.stopPropagation();
			toggleCommit(commit);
		});

		const label = document.createElement("button");
		label.type = "button";
		label.className = "commit-label";
		label.textContent = `${commit.short} ${commit.subject}`;
		label.addEventListener("click", () => selectCommit(commit.hash));

		row.append(toggle, label);
		els.commitList.append(row);
	}
}

function renderFiles() {
	const snapshot = view.snapshot;
	if (!snapshot) return;
	els.fileTree.textContent = "";
	for (const file of snapshot.files || []) {
		const row = document.createElement("button");
		row.type = "button";
		row.className = "file-row";
		if (!view.report && file.path === view.file) row.classList.add("selected");
		const path = document.createElement("span");
		path.className = "path";
		path.textContent = file.path;
		const counts = document.createElement("span");
		counts.className = "counts";
		counts.textContent = `+${file.additions} -${file.deletions}`;
		row.append(path, counts);
		row.addEventListener("click", () => selectFile(file.path));
		els.fileTree.append(row);
	}
	if (snapshot.report?.exists) {
		const row = document.createElement("button");
		row.type = "button";
		row.className = "file-row";
		if (view.report) row.classList.add("selected");
		const path = document.createElement("span");
		path.className = "path";
		path.textContent = "report";
		row.append(path);
		row.addEventListener("click", selectReport);
		els.fileTree.append(row);
	}
}

function renderEvidence() {
	const snapshot = view.snapshot;
	els.evidence.textContent = "";
	if (!snapshot) return;
	const hashes = view.commit ? [view.commit] : (snapshot.commits || []).map((c) => c.hash);
	let shown = 0;
	for (const hash of hashes) {
		const evidence = snapshot.evidence?.[hash];
		if (!evidence) continue;
		shown += 1;
		els.evidence.append(evidenceCard(hash, evidence));
	}
	if (!shown) {
		const empty = document.createElement("p");
		empty.className = "muted";
		empty.textContent = "No recorded verification for the selected commit.";
		els.evidence.append(empty);
	}
}

function evidenceCard(hash, evidence) {
	const card = document.createElement("div");
	card.className = "evidence-card";
	const title = document.createElement("div");
	title.className = `status-${evidence.status}`;
	title.textContent = `${evidence.status} · ${Number(evidence.duration || 0).toFixed(1)}s`;
	card.append(title);
	const meta = document.createElement("div");
	meta.className = "muted";
	meta.textContent = `commit ${shortHash(hash)}${evidence.node ? ` · node ${evidence.node}` : ""}`;
	card.append(meta);
	if (evidence.commands) {
		const commands = document.createElement("div");
		commands.className = "muted";
		try {
			const parsed = JSON.parse(evidence.commands);
			commands.textContent = Array.isArray(parsed) ? parsed.join(" · ") : String(evidence.commands);
		} catch {
			commands.textContent = String(evidence.commands);
		}
		card.append(commands);
	}
	if (evidence.fingerprint) {
		const fingerprint = document.createElement("button");
		fingerprint.type = "button";
		fingerprint.textContent = `fp ${String(evidence.fingerprint).slice(0, 12)}`;
		fingerprint.title = "Copy the full fingerprint";
		fingerprint.addEventListener("click", async () => {
			try {
				await navigator.clipboard.writeText(String(evidence.fingerprint));
				notice("fingerprint copied");
			} catch {
				notice(String(evidence.fingerprint));
			}
		});
		card.append(fingerprint);
	}
	if (evidence.output) {
		const pre = document.createElement("pre");
		pre.textContent = evidence.output;
		card.append(pre);
	}
	return card;
}

function renderComments() {
	const snapshot = view.snapshot;
	els.comments.textContent = "";
	const comments = snapshot?.comments || [];
	els.commentCount.textContent = comments.length ? `(${comments.length})` : "";
	for (const comment of comments) {
		const card = document.createElement("div");
		card.className = "comment";
		const where = document.createElement("div");
		where.className = "muted";
		const parts = [
			comment.commit_hash ? shortHash(comment.commit_hash) : "report",
			comment.file,
			comment.line ? `${comment.side}:${comment.line}` : null,
		]
			.filter(Boolean)
			.join(" ");
		where.textContent = `${parts || "general"} · ${comment.status}`;
		const body = document.createElement("div");
		body.textContent = comment.body;
		card.append(where, body);
		els.comments.append(card);
	}
}

function renderStatusbar() {
	const snapshot = view.snapshot;
	if (!snapshot) return;
	let additions = 0;
	let deletions = 0;
	for (const file of snapshot.files || []) {
		additions += file.additions;
		deletions += file.deletions;
	}
	els.statusbar.textContent =
		`${(snapshot.files || []).length} files · ${additions} additions · ` +
		`${deletions} deletions · ${snapshot.branch_key}`;
}

function render() {
	renderTopbar();
	renderCommits();
	renderFiles();
	renderEvidence();
	renderComments();
	renderStatusbar();
	markAnchor(els.diff, view.anchor, view.range);
}

// ---------------------------------------------------------------------------
// Selection and actions
// ---------------------------------------------------------------------------
async function loadDiff() {
	if (view.report) {
		els.diff.hidden = true;
		els.report.hidden = false;
		els.report.textContent = view.snapshot?.report?.content || "(no report generated)";
		els.diffHeader.textContent = "report";
		return;
	}
	els.diff.hidden = false;
	els.report.hidden = true;
	if (!view.file) {
		els.diffHeader.textContent = "Select a file to view its diff.";
		els.diff.textContent = "";
		return;
	}
	try {
		const result = await apiGet("/api/diff", {
			plane: view.plane,
			commit: view.commit,
			file: view.file,
		});
		els.diffHeader.textContent = `${view.commit ? shortHash(view.commit) + " " : ""}${view.file}`;
		renderDiff(els.diff, result.lines || [], {
			onSelect: (anchor, { extend }) => {
				if (extend && view.anchor && view.anchor.side === anchor.side) {
					view.range = anchor;
				} else {
					view.anchor = anchor;
					view.range = null;
				}
				markAnchor(els.diff, view.anchor, view.range);
			},
			onComment: openComment,
		});
		markAnchor(els.diff, view.anchor, view.range);
	} catch (error) {
		notice(error.message, true);
	}
}

function selectCommit(hash) {
	view.commit = hash;
	view.report = false;
	view.file = "";
	view.anchor = null;
	view.range = null;
	writeHash();
	poll();
}

function selectFile(path) {
	view.file = path;
	view.report = false;
	view.anchor = null;
	view.range = null;
	writeHash();
	renderFiles();
	loadDiff();
}

function selectReport() {
	view.report = true;
	view.file = "";
	writeHash();
	renderFiles();
	loadDiff();
}

function openComment(anchor) {
	view.anchor = anchor;
	view.range = view.range && view.range.side === anchor.side ? view.range : null;
	els.commentForm.hidden = false;
	els.commentAnchor.textContent = `${view.file}:${anchor.side}:${anchor.line}`;
	els.commentBody.focus();
	markAnchor(els.diff, view.anchor, view.range);
}

async function submitComment() {
	const body = els.commentBody.value.trim();
	if (!body) {
		notice("write a comment first", true);
		return;
	}
	try {
		await apiPost("comment", {
			commit: view.report ? null : view.commit || null,
			file: view.report ? null : view.file || null,
			side: view.report ? null : view.anchor?.side || null,
			line: view.report ? null : view.anchor?.line ?? null,
			line_end: view.report ? null : view.range?.line ?? view.anchor?.line ?? null,
			body,
		});
		els.commentBody.value = "";
		els.commentForm.hidden = true;
		notice("comment saved");
		await poll();
	} catch (error) {
		notice(error.message, true);
	}
}

async function toggleCommit(commit) {
	try {
		if (commit.approved) {
			const note = window.prompt("Request changes: describe what must change.");
			if (note === null) return;
			await apiPost("decision", { decision: "request_changes", commit: commit.hash, note });
		} else {
			await apiPost("decision", { decision: "approve", commit: commit.hash });
		}
		await poll();
	} catch (error) {
		notice(error.message, true);
	}
}

async function approveAll() {
	const snapshot = view.snapshot;
	if (!snapshot) return;
	const ok = window.confirm(
		`Approve all ${(snapshot.commits || []).filter((c) => !c.approved).length} unapproved commits?`,
	);
	if (!ok) return;
	try {
		await apiPost("decision", { decision: "approve", all: true });
		notice("all commits approved");
		await poll();
	} catch (error) {
		notice(error.message, true);
	}
}

async function deliver() {
	try {
		const result = await apiPost("deliver", {});
		const results = result.result?.results || [];
		const failed = results.some((row) => row.status === "failed");
		notice(failed ? "delivery failed; see the results" : "delivered");
		await poll();
	} catch (error) {
		notice(error.message, true);
	}
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------
async function poll() {
	try {
		const snapshot = await apiGet("/api/state", { plane: view.plane, commit: view.commit });
		view.snapshot = snapshot;
		if (snapshot.plane) view.plane = snapshot.plane;
		if (!view.pinned) {
			view.pinned = { source_tip: snapshot.source_tip, target_tip: snapshot.target_tip };
		}
		view.stale =
			view.pinned.source_tip !== snapshot.source_tip ||
			view.pinned.target_tip !== snapshot.target_tip;
		if (!view.file && !view.report && (snapshot.files || []).length) {
			view.file = snapshot.files[0].path;
			writeHash();
		}
		render();
		await loadDiff();
	} catch (error) {
		notice(error.message, true);
	}
}

function reloadPacket() {
	view.pinned = null;
	view.stale = false;
	poll();
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
els.approveAll.addEventListener("click", approveAll);
els.deliver.addEventListener("click", deliver);
els.stale.addEventListener("click", reloadPacket);
els.commentForm.addEventListener("submit", (event) => {
	event.preventDefault();
	submitComment();
});
els.commentCancel.addEventListener("click", () => {
	els.commentForm.hidden = true;
});
els.commentBody.addEventListener("keydown", (event) => {
	if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
		event.preventDefault();
		submitComment();
	}
	if (event.key === "Escape") {
		event.preventDefault();
		els.commentForm.hidden = true;
	}
});

readHash();
writeHash();
poll();
setInterval(poll, 5000);
