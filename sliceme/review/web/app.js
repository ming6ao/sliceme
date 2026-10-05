// Sliceme review client: per-commit approvals, the report, and the action calls.
//
// No build step and no framework. The write token arrives in the URL fragment
// (`#token=...`) and is sent as `X-Sliceme-Token` on every write. Selection
// updates use `history.replaceState`, so the token never enters history.

import { markAnchor, renderDiff } from "/diff.js";
import { renderMarkdown } from "/markdown.js";

const els = {
	targetName: document.getElementById("target-name"),
	campaignSelect: document.getElementById("campaign-select"),
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
	preview: document.getElementById("preview"),
	viewToggle: document.getElementById("view-toggle"),
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
	campaign: "",
	commit: "",
	file: "",
	report: false,
	preview: false,
	previewKey: "",
	snapshot: null,
	tips: null,
	anchor: null,
	range: null,
	refreshPending: false,
};

// ---------------------------------------------------------------------------
// Hash and API
// ---------------------------------------------------------------------------
function readHash() {
	const params = new URLSearchParams(location.hash.replace(/^#/, ""));
	if (params.get("token")) view.token = params.get("token");
	if (params.get("plane")) view.plane = params.get("plane");
	if (params.get("campaign")) view.campaign = params.get("campaign");
	view.commit = params.get("commit") || "";
	view.file = params.get("file") || "";
	view.report = params.get("report") === "1";
	view.preview = params.get("preview") === "1";
}

function writeHash() {
	const params = new URLSearchParams();
	if (view.token) params.set("token", view.token);
	if (view.plane) params.set("plane", view.plane);
	if (view.campaign) params.set("campaign", view.campaign);
	if (view.commit) params.set("commit", view.commit);
	if (view.file) params.set("file", view.file);
	if (view.report) params.set("report", "1");
	if (view.preview) params.set("preview", "1");
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
		body: JSON.stringify({ action, params: { plane: view.plane, campaign: view.campaign, ...params } }),
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

function isMarkdown(path) {
	return /\.(md|markdown|mdown|mkd)$/i.test(path || "");
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function renderCampaignSelect() {
	const snapshot = view.snapshot;
	const campaigns = snapshot?.campaigns || [];
	const select = els.campaignSelect;
	select.textContent = "";
	if (campaigns.length <= 1) {
		select.hidden = true;
		return;
	}
	for (const campaign of campaigns) {
		const option = document.createElement("option");
		option.value = campaign.key;
		option.textContent = campaign.target_branch || campaign.key;
		if (campaign.key === (snapshot.campaign || view.campaign)) option.selected = true;
		select.append(option);
	}
	select.hidden = false;
}

function renderTopbar() {
	const snapshot = view.snapshot;
	if (!snapshot) return;
	renderCampaignSelect();
	els.targetName.textContent = `sliceme review · ${snapshot.feature_branch}`;
	els.tips.textContent = `${shortHash(snapshot.source_tip)} → ${shortHash(snapshot.target_tip)}`;
	const unapproved = (snapshot.commits || []).filter((commit) => !commit.approved).length;
	els.approvalState.textContent = snapshot.all_approved
		? "all commits approved"
		: `${unapproved} unapproved`;
	els.stale.hidden = !view.refreshPending;
	els.stale.textContent = "new commits — refresh";
	els.approveAll.disabled = unapproved === 0;
	els.deliver.hidden = !snapshot.all_approved;
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
	els.diff.hidden = false;
	els.report.hidden = true;
	els.preview.hidden = true;
	els.viewToggle.hidden = true;

	if (view.report) return showReport();
	if (!view.file) {
		els.diffHeader.textContent = "Select a file to view its diff.";
		els.diff.textContent = "";
		return;
	}
	const markdown = isMarkdown(view.file);
	els.viewToggle.hidden = !markdown;
	els.viewToggle.textContent = view.preview ? "View diff" : "View rendered";
	els.diffHeader.textContent = `${view.commit ? shortHash(view.commit) + " " : ""}${view.file}`;
	if (markdown && view.preview) return showPreview();
	return showDiff();
}

function showReport() {
	els.diff.hidden = true;
	els.report.hidden = false;
	const content = view.snapshot?.report?.content || "";
	if (content.trim()) renderMarkdown(els.report, content);
	else els.report.textContent = "(no report generated)";
	els.diffHeader.textContent = "report";
}

async function showPreview() {
	els.diff.hidden = true;
	els.preview.hidden = false;
	const key = `${view.commit}|${view.file}`;
	if (view.previewKey === key) return;
	view.previewKey = key;
	try {
		const result = await apiGet("/api/file", {
			plane: view.plane,
			campaign: view.campaign,
			commit: view.commit,
			file: view.file,
		});
		renderMarkdown(els.preview, result.content || "");
	} catch (error) {
		notice(error.message, true);
		els.preview.textContent = "(cannot load the file)";
	}
}

async function showDiff() {
	try {
		const result = await apiGet("/api/diff", {
			plane: view.plane,
			campaign: view.campaign,
			commit: view.commit,
			file: view.file,
		});
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
	view.preview = false;
	view.previewKey = "";
	view.file = "";
	view.anchor = null;
	view.range = null;
	writeHash();
	poll();
}

function selectFile(path) {
	view.file = path;
	view.report = false;
	view.preview = isMarkdown(path);
	view.previewKey = "";
	view.anchor = null;
	view.range = null;
	writeHash();
	renderFiles();
	loadDiff();
}

function selectReport() {
	view.report = true;
	view.preview = false;
	view.previewKey = "";
	view.file = "";
	writeHash();
	renderFiles();
	loadDiff();
}

function togglePreview() {
	view.preview = !view.preview;
	view.previewKey = "";
	view.anchor = null;
	view.range = null;
	writeHash();
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
function tipsDiffer(a, b) {
	return a.source_tip !== b.source_tip || a.target_tip !== b.target_tip;
}

/**
 * Keep the selection when the new packet still holds it. A campaign only adds
 * commits, so a selected commit normally stays. A vanished file falls back to
 * the first file of the new packet. Return true when the selection changed.
 */
function reconcileSelection(snapshot) {
	let changed = false;
	const commits = snapshot.commits || [];
	if (view.commit && !commits.some((commit) => commit.hash === view.commit)) {
		view.commit = commits.length ? commits[commits.length - 1].hash : "";
		view.anchor = null;
		view.range = null;
		changed = true;
	}
	if (view.report) return changed;
	const files = snapshot.files || [];
	if (view.file && files.some((file) => file.path === view.file)) return changed;
	view.file = files.length ? files[0].path : "";
	view.preview = isMarkdown(view.file);
	view.previewKey = "";
	view.anchor = null;
	view.range = null;
	return true;
}

async function applySnapshot(snapshot) {
	view.snapshot = snapshot;
	view.tips = { source_tip: snapshot.source_tip, target_tip: snapshot.target_tip };
	view.refreshPending = false;
	if (reconcileSelection(snapshot)) writeHash();
	render();
	await loadDiff();
}

async function poll() {
	try {
		const snapshot = await apiGet("/api/state", {
			plane: view.plane,
			campaign: view.campaign,
			commit: view.commit,
		});
		if (snapshot.plane) view.plane = snapshot.plane;
		if (snapshot.campaign) view.campaign = snapshot.campaign;
		const tipsChanged = view.tips ? tipsDiffer(view.tips, snapshot) : false;
		if (tipsChanged && !els.commentForm.hidden) {
			// A comment draft is open. Keep the diff and the anchor stable until
			// the reviewer submits or cancels the comment.
			view.refreshPending = true;
			renderTopbar();
			return;
		}
		await applySnapshot(snapshot);
		if (tipsChanged) notice("new commits: refreshed");
	} catch (error) {
		notice(error.message, true);
	}
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
els.approveAll.addEventListener("click", approveAll);
els.deliver.addEventListener("click", deliver);
els.viewToggle.addEventListener("click", togglePreview);
els.campaignSelect.addEventListener("change", () => {
	view.campaign = els.campaignSelect.value;
	view.commit = "";
	view.file = "";
	view.report = false;
	view.preview = false;
	view.previewKey = "";
	view.anchor = null;
	view.range = null;
	writeHash();
	void poll();
});
els.commentForm.addEventListener("submit", (event) => {
	event.preventDefault();
	submitComment();
});
els.commentCancel.addEventListener("click", () => {
	els.commentForm.hidden = true;
	if (view.refreshPending) void poll();
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
setInterval(poll, 3000);
