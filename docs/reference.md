# Sliceme reference

Action reference, module map, state layout, verification semantics, tests, and
deliberate gaps. For the model and workflow, see [guide.md](./guide.md).

## 1. Actions

The CLI and the pi `sliceme` tool derive from one action registry
(`sliceme/surface.py`). The engine has eleven verbs:

| Action | Purpose |
|---|---|
| `start` (alias `init`) | Bootstrap the plane and a campaign for the current directory (idempotent). |
| `status` | Campaigns, records, waves, checks, health, simulation; the default is the dense summary, `--verbose` is the full dump, and `--sessions`/`--resume` cover the campaign registry and the resume plan. |
| `ready` | The current-wave node ids, the wave index, and `paused`. |
| `plan` | Parse the design's campaign split and join the registry state. |
| `deliver` | Push the work branch and open the delivery pull request against the base branch after the confirmation. |
| `check` | Run the synchronous combined-tree checks for the current wave. |
| `open` | Create or reuse the single campaign worktree. |
| `record` | Record the current wave: conformance + per-node commits. |
| `approve` | Record one campaign approval. |
| `report` | Write the deterministic campaign report. |
| `evidence` | Write the deterministic evidence document: the commits, the checks, the diffs, and the worker logs. |

The pi extension forwards these verbs through one tool. The campaign loop lives
in the `sliceme.campaign` workflow resource, not in the engine.

Most campaign-scoped actions accept `--campaign REF`. The reference is a
work branch, a branch key, or a unit name. When a plane holds one campaign,
`--campaign` is optional. When a plane holds several campaigns, a
campaign-scoped call without `--campaign` returns the plane summary. See §3 for
the layout.

### `start`

```bash
sliceme start [--path DIR]
                [--design DESIGN.md] [--feature-branch BRANCH] [--base REF]
                [--check NAME=COMMAND ...]
                [--force] [--campaign REF]
```

Idempotent bootstrap: Sliceme writes `.sliceme/config.json` and
`.sliceme/state.db` when the plane does not exist. It also adds `.sliceme/` to
the repo-local `.git/info/exclude`. It registers one campaign for the design or
the work branch; the campaign owns the one worktree, which `open`
creates. Re-running `start` is a no-op for the same campaign.

- `--design DESIGN.md` names the design document. The engine derives the
  **work branch** `feat/<slug(design-stem)>` from its file name and records
  it. `--feature-branch BRANCH` overrides the derived name. One of the two is
  required when the plane does not exist.
- The work branch is the **pull request head**. It is **never** `main`,
  `master`, or the repository default branch; `start` and `deliver` refuse it.
  There is no override.
- `--base REF` records the **base branch** (the pull request base). The
  default is the repository default branch (`main`).
- The campaign worktree uses the work branch. `open`
  fetches the base branch and creates the worktree from `origin/<base>`.
- `--check NAME=COMMAND` registers a trusted plane check (repeatable).
- Sliceme captures the recorded `default_branch` once at init: origin `HEAD`,
  else `init.defaultBranch`, else an existing `main` or `master`, else `main`.
- Re-running `start` with a different `--feature-branch` adds a campaign to the
  same plane; the earlier campaigns stay intact.

The programmatic plane-only helper is `Service.init_plane(root, ...)`.

### `status`

```bash
sliceme status [--unit U] [--short] [--dense] [--verbose] [--simulate] [--no-checks]
                 [--health] [--gc] [--sessions] [--resume [--plan-only]] [--campaign REF]
```

The default human output is the **dense summary**. It has a header, one line per
node in DAG order, and one line per wave. The header shows `campaign`,
`target`/`worktree`/`base`, `design`, node count, and wave size. A node line
shows `w<wave> <id> <status>` plus the node label. A wave line shows
the index, the status, and the member ids.

`--dense` asks for it explicitly and `--verbose` prints the full nested dump
instead. With `--json` the default is the nested dump; `--dense --json` emits
the summary as JSON (including `lines`). `--short` prints only the bound
campaign's name (or key). `--health` checks git/config/db.

`--gc` prunes worktrees, delivered-work branches, scratch, expired review
rows, and the files of every campaign that has finished. It keeps the report.
`--simulate` groups recorded records into DAG waves, materializes each
wave's combined tree, and runs the configured checks once over the combined
result; `--no-checks` plans only. `--sessions` lists registered campaigns from
their descriptor files. `--resume` reconciles a suspended campaign from git plus
`state.db` and returns its resume plan; `--plan-only` is a compatibility no-op,
because the resume plan is a pure read.

The nested dump (`--verbose`, or `--json` without `--dense`) returns
`dag_waves` (the scheduler's wave plan), `ready` (the ready node ids), `paused`,
`checks` (check counts), and the campaign writer's columns (`node`, `log`,
`record`, `verification`). The dense summary returns the header and the
node/wave lines instead.

Every projection first **normalizes** `dag.json`. Sliceme contracts
same-ownership nodes, then computes the waves. A campaign can never schedule an
un-normalized DAG. Two nodes merge when they share an owned directory and one
depends on the other. The survivor is the node with the lowest topological
index.

The merged node owns the union of the directories, waits on the union of the
external dependencies, and concatenates the goals and the acceptance commands.
Sliceme rewrites an absorbed id in any `depends_on` to the survivor. It records
the absorbed ids in `merged_from`. Sliceme protects a node that has a recorded
record or a status other than `pending`. Sliceme reports the result as
`dag_merge` (`merged`, `before_nodes`, `after_nodes`, `before_waves`,
`after_waves`).

### `ready`

```bash
sliceme ready [--campaign REF]
```

Returns `campaign`, `ready` (the current-wave node ids that are neither
recorded nor running), `wave` (the current wave index), and `paused`. The
workflow resource polls this verb, so its fields are the loop contract. The wave
is the one scheduling gate: every dependency lives in an earlier wave, so a node
is ready once the engine reaches its wave and the node itself is neither
recorded nor running. Sliceme scopes the result to the current wave, so a caller
spawns and records one wave per call. A missing DAG reports no ready nodes.

### `plan`

```bash
sliceme plan --design DESIGN.md
```

Read the `sliceme-campaigns` fenced block from the design document. Join the
entries, in order, with the campaign registry state. Print each entry's name,
target, base, directory scope, and state. Print `next`: the first entry whose
campaign is not delivered or closed.

Use the plan to run one design as a sequence of campaigns. Each campaign has its
own DAG and worktree, so a directory may repeat across entries. The engine keeps
one owner per directory inside one campaign. See
[multi-campaign.md](./multi-campaign.md) section 15.

### `deliver`

```bash
sliceme deliver [--source BRANCH]
                   [--cleanup none|worktrees|all] [--no-checks] [--campaign REF]
```

- Pushes the work branch (`--source`, default the recorded
  `work_branch`) to the remote
  (`policy.remote`, default `origin`).
- Opens one pull request against the **base branch** (the repository default
  branch, `main`) with the `gh` program. The body is the evidence document.
- Pre-checks the merge with `git merge-tree`. A conflict returns structured
  findings before the push.
- Runs the plane's trusted checks on the campaign head through the single check
  runner; a failed check stops the delivery before the push. The runner caches
  the verdict by fingerprint.
- Marks recorded records and their unit `delivered` without rewriting their
  recorded commits.

It is idempotent: Sliceme returns an open pull request as it is, and a delivery
base that already contains the work branch is a no-op. Delivery needs the
campaign work branch; run `open` first. It refuses until the newest
campaign decision is an unconsumed `approve`, or an `override` records a note.
Install `gh` and authenticate it before the first delivery.

Sliceme resolves `gh` in this order:

1. the `SLICEME_GH` environment variable, when set;
2. the process `PATH`;
3. the common install directories (`~/.local/bin`, Homebrew, MacPorts, `snap`,
   `/usr/local/bin`, `/usr/bin`).

Step 3 covers a process that starts from a desktop launcher or a service and has
a small `PATH`. Set `SLICEME_GH` when `gh` lives elsewhere. Sliceme validates
`gh` before the push, so a missing program leaves the campaign working.

**The work branch is never the repository default branch.** Sliceme refuses
to push `main`, `master`, or the recorded default, with **no override**.
Promotion from the work branch to the default branch stays a human act on
the forge.

### `check`

```bash
sliceme check [--campaign REF]
```

Run the plane's trusted checks once over the current wave's combined tree. The
wave tree is the campaign worktree head after `record`, so the checks run
over the recorded nodes together. The verb reports `wave`, `members`, and the
row (`fingerprint`, `status`, `duration`, `exit_code`, `output`, `results`,
`cached`). The cache serves a fingerprint that already has a terminal verdict,
so a resumed node reads the cache. The verb always acts on the current wave; the
engine reads the wave index from its own state.

### `open`

```bash
sliceme open
```

Create (or reuse) the single **campaign worktree** and its branch
(`work_branch`, for example `feat/<name>`), idempotently. It fetches the base
branch and bases the worktree on `origin/<base_branch>`, then reports a fallback
when the remote or the base is absent. Sliceme uses the same worktree for every
wave and never recreates it between waves.

### `record`

```bash
sliceme record
```

Record the current wave. Ownership syntax is a repository-relative directory
path; Sliceme also accepts a trailing slash. Sliceme rejects a kind prefix
(`dir:`, `file:`, `symbol:`, …) and an empty or blank entry when it projects the
DAG. A node that changes no file may omit `owns`. `record` stages the campaign worktree, enforces
conformance-by-ownership for the wave, and creates one commit + recorded record
per changed node. Sliceme rejects unowned, ambiguous, or cross-node-rename
changes, and records an explicit empty marker for a member that changed no file.
It diffs against the current `HEAD`, so it never re-attributes an earlier wave's
committed changes.

The commit subject falls back to the DAG node `label`. A node with changes and
no `label` is an error. The subject never carries a wave prefix. The node and
the wave stay in `state.db` and the DAG.

The recorder holds the campaign lock, so it serializes with campaign creation
and other records. Checks are synchronous and run inside the caller, so they
need no lock.

### `evidence`

```bash
sliceme evidence [--design REF] [--campaign REF]
```

Write the deterministic evidence document for the campaign. The engine writes
`.sliceme/<branch-key>.evidence.json` (the complete evidence, with the full check
output) and `.sliceme/<branch-key>.evidence.md` (the check output is bounded).
The Markdown document is the pull request body. The evidence holds:

- the design reference, the work branch, and the base branch;
- every campaign commit, oldest first;
- the node, the goal, and the owned directories for each commit;
- the newest check for each commit (status, command, fingerprint, duration, and
  output);
- the diffstat and the changed files for each commit;
- the worker log path, and the tail of the log when a writer recorded one;
- the artifact paths and the report path.

The narrative is deterministic and the engine writes it; there is no narrative
agent. The evidence is not a gate.

### `approve`

```bash
sliceme approve [--all] [--commit SHA] [--note TEXT] [--actor A]
```

Append one **campaign-level** approval (`commit_hash` is null). One approval
covers the whole accumulated commit set. The `--all` and `--commit` flags stay
for compatibility; they do not change the stored approval.

### `report`

```bash
sliceme report [--narrative TEXT] [--design REF]
```

Write `.sliceme/<branch-key>.report.md`: a deterministic skeleton plus an
optional `--narrative`. See `docs/review.md` for the approval gate and the
report.

Delivery proceeds only while the newest campaign approval is an unconsumed
`approve`, or an `override` that records a note. A successful delivery consumes
the approval it used. A failed check or forge call leaves the approval
unconsumed, so a retry needs no new review.

### Sandbox manifests

The target repository provides how to run checks in isolation as a tracked
manifest at the repo root: `sliceme.sandbox.json`, `.sliceme-sandbox.json`, or
`tools/sliceme-sandbox.json` (never under `.sliceme/`, which is git-excluded).
Schema: `version`, `command` (prefix receiving `/bin/sh -lc "<cmd>"`),
`network`, `readonly_repo`, `writable`, `setup`, and `gpu`
(`{command, tiers}`).

Resolution: `--sandbox` > `dag.json.sandbox` > `policy.sandbox` > discovered
manifest > `none`. The planner records `"sandbox": {"path": ...}` in `dag.json`.
The check runner resolves the gate before every run and records the
`sandbox_digest`. `policy.require_sandbox` (or `dag.json.sandbox_required`)
fails closed when no profile exists. Sliceme re-checks a manifest `digest`
recorded in `dag.json`, so Sliceme rejects a post-plan manifest change.

## 2. Module map

| Module | Responsibility |
|---|---|
| `sliceme/surface.py` | **single source of truth**: action registry, validation, dispatch |
| `sliceme/cli.py` | generated `argparse` CLI (`sliceme`), human + `--json` output |
| `sliceme/service.py` | **verb facade**: composes the verb-group mixins and owns the bound campaign |
| `sliceme/verbs/` | the verb groups: `bootstrap`, `campaign`, `status`, `review`, `delivery`, `sessions` |
| `sliceme/store.py` | SQLite persistence (WAL) |
| `sliceme/gitutil.py` | Git plumbing (`worktree`, `merge`, `merge-tree`, `commit`, `branch`, `push`, `changed_files`) |
| `sliceme/ownership.py` | Directory ownership (normalization, `owns`, subtree conflicts), the DAG wave projection, GPU isolation, and the same-ownership merge |
| `sliceme/plan.py` | the design's `sliceme-campaigns` split: parse and validate |
| `sliceme/verifier.py` | Fingerprints and the sandboxed check runner |
| `sliceme/sandbox.py` | Isolation profiles + project manifests (`none`/`bwrap`/`unshare`/`command`), the gate, and command wrapping |
| `sliceme/checks.py` | The single synchronous combined-tree check runner plus the `checks` cache |
| `sliceme/integrate.py` | Work-branch guard and the base branch, pull request delivery, and combined-tree simulation |
| `sliceme/pullrequest.py` | The `gh` forge client: find or create the delivery pull request |
| `sliceme/campaign.py` | `dag.json` layout and readers; the deterministic report and evidence document |
| `sliceme/review/` | Reduced review: `packet.py` (snapshot + commits + report + evidence), `diff.py` (diff parsing) |
| `integrations/pi/` | the pi adapter: `common.ts` (invocation and paths), `coordinator.ts` (tool, agents, commands), `campaign-resource.ts` (the `sliceme.campaign` resource), `agents/{planner,worker}.md` |

The engine is dependency-free Python 3.11+. `Service` is the only state owner;
adapters only parse arguments and render results. `bin/sliceme` is a shim so the
CLI runs without installation.

## 3. State layout

All campaign state lives under `.sliceme/`, **prefixed by the work-branch
name** so one campaign's files form a single glob and no two campaigns collide.
Let `branch-key` replace `/` with `--` (`feat/x` → `feat--x`):

```text
.sliceme/
  config.json                      # plane config (default_branch, checks, policy)
  state.db                         # campaigns, records, verdicts, approvals (SQLite, WAL)
  workspace.lock                   # the one workspace lock (campaign create, record, delivery)
  active.<pid>.campaign            # per-process pointer to the session's campaign
  feat--x.dag.json                 # canonical plan (never committed)
  feat--x.report.md                # deterministic report (kept on cleanup)
  feat--x.evidence.json            # complete evidence (full check output)
  feat--x.evidence.md              # human evidence; the pull request body
  feat--x.session.json             # adapter-written suspend/resume descriptor
  feat--x.control.json             # cooperative pause flag
  feat--x.worker_<id>.log          # one log per worker id
  worktrees/                       # the single campaign worktree
  scratch/                         # detached simulation and check worktrees (transient)
```

The adapter-written `.session.json` descriptor is the one place a node's status
lives: its `nodes` map records `pending|running|recorded|failed` (and an
`attempts` counter) at suspend time. There is no `state.json`. Git and
`state.db` are authoritative. Only the pi adapter writes the descriptor and the
`.control.json` pause flag; the engine reads them (`status --resume`,
`status --sessions`, `ready`).

SQLite tables: `campaigns`, `records`, `verdicts`, `approvals`.

## 4. Verification

`fingerprint = sha256(tree, cmd_digest, toolchain_digest, policy_digest,
sandbox_digest, checks_digest, source)`:

- `tree`: the campaign head tree at the recorded commit;
- `cmd_digest`: the command vector — the plane's configured checks, or a node's
  acceptance commands;
- `toolchain_digest`: `git --version`, Python version, and hashed lockfiles
  (`package-lock.json`, `Cargo.lock`, `go.sum`, `poetry.lock`, …);
- `policy_digest`: policy block of the config;
- `sandbox_digest`: the resolved isolation profile (`sliceme/sandbox.py`), so a
  stricter sandbox invalidates a cached verdict;
- `checks_digest`: the check-runner semantics version, so changing how checks run
  invalidates cached verdicts;
- `source`: `plane`, `wave:<n>`, or `deliver`, so unrelated verdicts cannot
  collide.

Checks run in a clean detached scratch worktree at the commit and, when a
sandbox exists, wrapped accordingly. The runner serves a cached terminal row
(`passed`, `failed`, or `error`) for an unchanged fingerprint, so a resumed
campaign re-verifies from the cache. Verification never mutates the record or
the base branch. The newest terminal check for a commit is the review
evidence. Agent-reported tests are provenance only, never acceptance.

The runner is **synchronous**. One call runs one check set and writes one
terminal row. There is no queue, no lease, and no second runner. The
`_dispatch_record` recorder holds the workspace lock, so recording and checks
never interleave.

## 5. Tests

```bash
npm test
```

The suite covers these areas:

- directory normalization and subtree conflicts, and the DAG wave projection;
- GPU isolation: two GPU nodes land in two waves;
- conformance-by-ownership at wave record time;
- the combined-tree check runner and its cache;
- campaign worktree recording and work-branch selection;
- the campaign approval gate and the report;
- the workflow resource grants and field rejection (Node harness);
- end-to-end flows (delivery, idempotency, conflict atomicity, failing checks,
  simulation, cleanup, and reporting);
- CLI and packaging smoke tests.

| File | Covers |
|---|---|
| `tests/test_scopes.py` | ownership normalization, `owns`, subtree conflicts |
| `tests/test_waves.py` | DAG wave projection, dependency barriers, caps, GPU isolation, validation |
| `tests/test_merge.py` | the same-ownership merge |
| `tests/test_plan.py` | the design's campaign split parse |
| `tests/test_campaign.py` | plane bootstrap and campaign registration, wave recording, report, DAG/state layout |
| `tests/test_campaigns.py` | several campaigns in one plane, scoping, delivery isolation, migration |
| `tests/test_checks.py` | the combined-tree runner, the check cache, sandbox profiles/wrapping, fingerprint invalidation |
| `tests/test_sandbox_gate.py` | manifest discovery/validation, fail-closed gate, GPU runner, setup |
| `tests/test_wave_scope.py` | campaign worktree reuse, conformance-by-ownership, per-node commits, delivery, default-branch refusal |
| `tests/test_target_branch.py` | the design-derived work branch, the `--feature-branch` override, the base branch, default-branch refusal |
| `tests/test_open_campaign.py` | the worktree from `origin/main` and the local-base fallback |
| `tests/test_evidence.py` | the evidence document: commits, checks, diffs, logs, and the bounded Markdown |
| `tests/test_e2e.py` | the full flow from `start` to the delivery pull request |
| `tests/test_pull_request.py` | the `gh` client, the evidence-backed body, the head/base, and the fail-closed rules |
| `tests/test_review_approval.py` | the one campaign approval, the override, and the retired comment verbs |
| `tests/test_review_report.py` | the deterministic report and the packet |
| `tests/test_sessions.py` | the descriptor round-trip, the resume plan, cleanup, and the additive `verdicts` table |
| `tests/test_status_summary.py` | the dense `status` summary |
| `tests/test_cli.py` | CLI surface and lifecycle |
| `tests/test_pi_package.py` | pi package contract, the tool/verbs, the resource, and the agent definitions |
| `tests/campaign_resource_test.mjs` | the resource fixed grants and bounded-field rejection |

## 6. Deliberate gaps

- Symbol/AST extraction is not implemented; directory ownership and
  `git merge-tree` are the detectors. Dependency edges beyond
  `owns`/`depends_on` are not inferred.
- No long-lived daemon or unix socket: the CLI calls the SQLite service directly
  (WAL). There is no browser review client.
- One campaign occupies one work branch; several campaigns can share a
  plane. RPC-steerable workers are out of scope.
- `jj` workspaces and shared dependency caches are not implemented. Campaign
  workers are pure editors in the single campaign worktree (`open` /
  `record`).
- Promotion from the work branch to the default branch is a human act on
  the forge.
- Child status, events, and control belong to pi-subagents. Sliceme reads the
  engine's own state and does not keep a per-child progress file.
