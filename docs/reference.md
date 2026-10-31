# Sliceme reference

> **Status.** This document describes the directory-slice design: one node per
> directory. The engine change is tracked in
> [directory-slices-plan.md](./directory-slices-plan.md). The GPU field is
> removed.

Action reference, module map, state layout, verification semantics, tests, and
deliberate gaps. For the model and workflow, see [guide.md](./guide.md).

## 1. Actions

The CLI and the pi `sliceme` tool derive from one action registry
(`sliceme/surface.py`). The engine has sixteen verbs:

| Action | Purpose |
|---|---|
| `start` (alias `init`) | Bootstrap the plane and a campaign for the current directory (idempotent). |
| `status` | The campaign state: records, phases, and checks. The default is the dense summary and `--verbose` is the full dump. |
| `sessions` | List the registered campaigns from their session descriptors. |
| `resume` | Reconcile a suspended campaign and return its resume plan. |
| `budget` | Set, raise, or clear the campaign wall-clock and cost caps. |
| `gc` | Prune worktrees, delivered branches, scratch, and finished campaign files. |
| `doctor` | Check git, `config.json`, and `state.db` health. |
| `ready` | The current-phase node ids, the node definitions, the plan file, the worktree path, the phase index, `paused`, and `pause_reason`; `--brief` leaves out the node text. |
| `plan` | Parse the design's campaign split and join the registry state. |
| `deliver` | Confirm and deliver: `--confirm` records the one campaign approval, then `pull_request` pushes the work branch and opens a pull request; `local` merges the work branch into the local base branch. `--narrative` is the summary paragraph of the pull request body. |
| `check` | Run the synchronous combined-tree checks for a recorded phase (`--phase`), with `--refresh` to bypass the cache. |
| `open` | Create or reuse the single campaign worktree. |
| `record` | Record the current phase, or reopen a recorded phase with `--reopen`. |
| `report` | Write the deterministic campaign report. |
| `evidence` | Write the deterministic evidence document: the commits, the checks, the diffs, and the worker logs. |

The pi extension forwards these verbs through one tool. The campaign loop lives
in the `sliceme.campaign` workflow resource, not in the engine.

Most campaign-scoped actions accept `--campaign REF`. The reference is a
work branch, a branch key, or a unit name. When a plane holds one campaign,
`--campaign` is optional. When a plane holds several campaigns, a
campaign-scoped call without `--campaign` returns the plane summary. See §3 for
the layout.

The command line prints the result before it exits. A raised `SlicemeError`
exits with code 2. A terminal verdict sets a different code: `check` exits with
1 for `failed` and 3 for `error`; `deliver` exits with 1 when a result is
`failed` and 3 when a result is `error`. Every other action exits with 0. A
failed check also prints the failed command names on standard error.

### `start`

```bash
sliceme start [--path DIR]
                [--design DESIGN.md] [--feature-branch BRANCH] [--base REF]
                [--mode pull_request|local]
                [--check NAME=COMMAND ...]
                [--budget-seconds N] [--budget-usd USD]
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
- `--mode pull_request|local` records the plane delivery mode. The default is
  `pull_request`. A `local` delivery merges the work branch into the local base
  branch and stops, so the user pushes the base branch later.
- The campaign worktree uses the work branch. `open`
  fetches the base branch and creates the worktree from `origin/<base>`.
- `--check NAME=COMMAND` registers a trusted plane check (repeatable).
- `--budget-seconds N` and `--budget-usd USD` set the campaign caps. Both are
  optional; a missing cap means "no limit". The cap stops the campaign at a
  phase boundary (`docs/sessions.md` §9).
- Sliceme captures the recorded `default_branch` once at init: origin `HEAD`,
  else `init.defaultBranch`, else an existing `main` or `master`, else `main`.
- Re-running `start` with a different `--feature-branch` adds a campaign to the
  same plane; the earlier campaigns stay intact.

The programmatic plane-only helper is `Service.init_plane(root, ...)`.

### `status`

```bash
sliceme status [--unit U] [--short] [--dense] [--verbose] [--campaign REF]
```

The default human output is the **dense summary**. It has a header, one line per
node in DAG order, and one line per phase. The header shows `campaign`, the work
branch, the worktree path, and `base`, plus `design`, node count, phase size, and
the phase count with the current phase. A node line shows `w<phase> <id> <status>`
plus the node label. A phase line shows the index, the status, and the member ids.

`--dense` asks for it explicitly and `--verbose` prints the full nested dump
instead. With `--json` the default is the nested dump; `--dense --json` emits
the summary as JSON (including `lines`). `--short` prints only the bound
campaign's name (or key). `--unit U` shows one campaign's dashboard.

The nested dump (`--verbose`, or `--json` without `--dense`) returns
`dag_phases` (the scheduler's phase plan), `ready` (the ready node ids), `paused`,
`checks` (check counts), and the campaign writer's columns (`node`, `log`,
`record`, `verification`). The dense summary returns the header and the
node/phase lines instead, plus `phase_count` and `current_phase` as numbers.

Every projection first **normalizes** `dag.json`. Sliceme contracts
same-directory nodes, then computes the phases. A campaign can never schedule an
un-normalized DAG. Two nodes merge when they share a directory and one
depends on the other. The survivor is the node with the lowest topological
index.

The merged node owns the directory, waits on the union of the
external dependencies, and concatenates the goals, the tasks, and the acceptance
commands. Sliceme rewrites an absorbed id in any `depends_on` to the survivor. It
records the absorbed ids in `merged_from`. Sliceme protects a node that has a
recorded record or a status other than `pending`. Sliceme reports the result as
`dag_merge` (`merged`, `before_nodes`, `after_nodes`, `before_phases`,
`after_phases`).

### `sessions`

```bash
sliceme sessions
```

List the registered campaigns from their descriptor files. This is the engine
view that a second terminal uses. Each entry carries `work_branch`, `campaign`,
`label`, `status`, `phase`, `done`, `total`, and `session_file`.

### `resume`

```bash
sliceme resume [--campaign REF]
```

Read `.sliceme/<branch-key>.session.json`, reconcile it with git plus
`state.db`, and return the resume plan. The plan is a pure read: it writes
nothing. Git and the database win over the descriptor.

### `budget`

```bash
sliceme budget [--seconds N] [--usd USD] [--clear] [--campaign REF]
```

Set, raise, or clear the caps the adapter enforces. The verb writes `budget` and
`budget_started_at` in `.sliceme/config.json`. `--clear` removes both caps. Each
call starts a new measurement window, so the adapter resets its spend counters.
The `budget` action also clears a budget pause flag, so the campaign can
continue.

### `gc`

```bash
sliceme gc
```

Prune worktrees, delivered-work branches, scratch, expired review rows, and the
files of every campaign that has finished. It keeps the report and the evidence
document. It keeps a landed campaign branch until its merge commit is on the
remote base branch.

### `doctor`

```bash
sliceme doctor
```

Check git, `config.json`, and `state.db`. The result carries one `checks` list
and one `ok` flag.

### `ready`

```bash
sliceme ready [--campaign REF] [--brief]
```

Returns `campaign`, `ready` (the current-phase node ids that are neither
recorded nor running), `nodes` (the definition of each ready node: `id`, `dir`,
`label`, `goal`, and `tasks`), `dag` (the plan file), and `worktree` (the
campaign worktree path, or null before `open`). It also returns `phase` (the
current phase index), `paused`, `pause_reason`, and `budget`.

The workflow resource polls this verb, so its fields are the loop contract. The
phase is the one scheduling gate. Every dependency lives in an earlier phase. A
node is ready once the engine reaches its phase, and the node is neither
recorded nor running. Sliceme scopes the result to the current phase, so a
caller spawns and records one phase per call. A missing DAG reports no ready
nodes.

`--brief` leaves `goal` and `tasks` out of each `nodes` entry. The workflow host
returns at most 4096 bytes of standard output, and one detailed node can exceed
that, so the resource reads the brief form. The worker then reads its own node
from the `dag` file, which is the only file it reads under `.sliceme/`.

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
[multi-campaign.md](./multi-campaign.md) section 12.

### `deliver`

```bash
sliceme deliver --confirm [--source BRANCH] [--mode pull_request|local]
                 [--narrative TEXT]
                   [--actor A] [--note TEXT]
                   [--cleanup none|worktrees|all] [--no-checks] [--campaign REF]
```

The delivery mode is the `--mode` value, then the campaign value, then the
plane value. The default is `pull_request`. Both modes keep the campaign branch
and the campaign evidence.

Sliceme folds the approval into delivery. `--confirm` is the human confirmation
token. Without it, `deliver` refuses with a `not_confirmed` finding. With it,
`deliver` appends one campaign-level `approve` row (`--actor` and `--note` join
the row), delivers, and consumes the row when the delivery lands. A failed
delivery marks the row void (`action='void'` and `consumed_at`), so the next call
needs a new confirmation. The void row stays for audit evidence.

- The `pull_request` mode pushes the work branch (`--source`, default the
  recorded `work_branch`) to the remote (`policy.remote`, default `origin`).
  It opens one pull request against the **base branch** (the repository
  default branch, `main`) with the `gh` program. The body is the summary and
  the verification (see `docs/review.md` §6). The complete evidence document
  stays local.
- The `local` mode merges the work branch into the local **base branch** with
  one merge commit. It pushes nothing and calls no forge. The user pushes the
  base branch later.
- Pre-checks the merge with `git merge-tree`. A conflict returns structured
  findings before the merge or the push.
- Runs the plane's trusted checks on the campaign head through the single check
  runner. A failed check stops the delivery. The runner caches the verdict by
  fingerprint.
- Marks recorded records `delivered` without rewriting their recorded commits.
  The campaign moves to `delivered` for a pull request and to `landed` for a
  local merge.

It is idempotent: Sliceme returns an open pull request as it is, and a delivery
base that already contains the work branch is a no-op. The result status names
the action:

- `delivered` for a new delivery;
- `already_landed` for a local merge that is already on the base;
- `already_contained` for a base that already holds the work;
- `pull_request_already_open` for an open pull request;
- `failed` for a refused delivery.

Delivery needs the campaign work branch; run `open` first. It refuses without
`--confirm`.

A `local` delivery refuses a base worktree with uncommitted changes, and it
refuses when the remote base branch is ahead of or has diverged from the local
base branch. After a local merge, `gc` keeps the campaign branch until the
merge commit is on the remote base branch.

Install `gh` and authenticate it before the first `pull_request` delivery.
The `pull_request` mode resolves `gh` in this order:

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
sliceme check [--phase N] [--refresh] [--campaign REF]
```

Run the plane's trusted checks once over one recorded phase's combined tree.
With no `--phase`, the verb uses the phase of the newest record and the current
worktree head. With `--phase N`, the verb uses the newest record `head_commit`
of phase N, so later phases do not change the tree under check. The verb
reports `phase`, `members`, `commit`, and the row (`fingerprint`, `status`,
`duration`, `exit_code`, `output`, `results`, `cached`). The cache serves a
fingerprint that already has a terminal verdict, so a resumed node reads the
cache. `--refresh` skips the cache and runs the checks again.

### `open`

```bash
sliceme open
```

Create (or reuse) the single **campaign worktree** and its branch
(`work_branch`, for example `feat/<name>`), idempotently. It fetches the base
branch and bases the worktree on `origin/<base_branch>`, then reports a fallback
when the remote or the base is absent. When the campaign branch already exists,
Sliceme compares it with the base:

- a branch behind the base with no commit of its own is fast-forwarded;
- a branch ahead of the base is accepted;
- a diverged branch is refused with both tips (reason `diverged_branch`).

Sliceme uses the same worktree for every phase and never recreates it between
phases.

### `record`

```bash
sliceme record [--reopen N] [--campaign REF]
```

Record the current phase. Ownership syntax is one repository-relative directory
path; Sliceme also accepts a trailing slash. Sliceme rejects a kind prefix
(`dir:`, `file:`, `symbol:`, …) and an empty or blank directory when it projects
the DAG. A node that changes no file may omit `dir`. `record` stages the
campaign worktree, maps every changed path to the deepest `dir`, enforces
conformance-by-ownership for the phase, and creates one commit + recorded record
per changed node. Sliceme rejects a stray, ambiguous, or cross-node-rename
change, and records an explicit empty marker for a member that changed no file.
It diffs against the current `HEAD`, so it never re-attributes an earlier phase's
committed changes.

`--reopen N` drops the records at or after phase N, resets the worktree to the
parent of the first phase-N commit, and leaves the cursor at N. The next
`record` records the phase again. The verb refuses a dirty worktree, a finished
campaign, and a dropped commit that is already on the remote. Live approval rows
become void.

The commit subject falls back to the DAG node `label`. A node with changes and
no `label` is an error. The subject never carries a phase prefix. The node and
the phase stay in `state.db` and the DAG.

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
The Markdown document is the local, complete record of the campaign. The
delivery pull request body is the shorter summary document (see `deliver`).
The evidence holds:

- the design reference, the work branch, and the base branch;
- every campaign commit, oldest first;
- the node, the goal, and the owned directories for each commit;
- the newest check for each commit (status, command, fingerprint, duration, and
  output);
- the diffstat and the changed files for each commit;
- the worker log path, and the tail of the log when a writer recorded one;
- the artifact paths and the report path.

The summary of the evidence is deterministic and the engine writes it; there
is no narrative agent. The evidence is not a gate.

### `report`

```bash
sliceme report [--narrative TEXT] [--design REF]
```

Write `.sliceme/<branch-key>.report.md`: a deterministic skeleton plus an
optional `--narrative`. See `docs/review.md` for the confirmation and the
report.

The `deliver --confirm` action records one campaign-level `approve` row. A
successful delivery consumes the row. A failed check or forge call leaves it
unconsumed, and the next delivery appends a new row.

### Sandbox manifests

The target repository provides how to run checks in isolation as a tracked
manifest at the repo root: `sliceme.sandbox.json`, `.sliceme-sandbox.json`, or
`tools/sliceme-sandbox.json` (never under `.sliceme/`, which is git-excluded).
Schema: `version`, `command` (prefix receiving `/bin/sh -lc "<cmd>"`),
`network`, `readonly_repo`, `writable`, and `setup`. Sliceme rejects the removed
`gpu` key.

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
| `sliceme/ownership.py` | Directory ownership (normalization, `dir`, subtree conflicts), the DAG phase projection, and the same-directory merge |
| `sliceme/plan.py` | the design's `sliceme-campaigns` split: parse and validate |
| `sliceme/verifier.py` | Fingerprints and the sandboxed check runner |
| `sliceme/sandbox.py` | Isolation profiles + project manifests (`none`/`bwrap`/`unshare`/`command`), the gate, and command wrapping |
| `sliceme/checks.py` | The single synchronous combined-tree check runner plus the `checks` cache |
| `sliceme/integrate.py` | Work-branch guard and the base branch, pull request and local-merge delivery |
| `sliceme/pullrequest.py` | The `gh` forge client: find or create the delivery pull request |
| `sliceme/campaign.py` | `dag.json` layout and readers; the deterministic report and evidence document |
| `sliceme/review/` | Reduced review: `packet.py` (snapshot + commits + report + evidence), `diff.py` (diff parsing) |
| `integrations/pi/` | the pi adapter: `common.ts` (invocation and paths), `budget.ts` (the pure cap and spend logic), `coordinator.ts` (tool, agents, commands, the budget watchdog), `plan-resource.ts` (the `sliceme.plan` resource), `campaign-resource.ts` (the `sliceme.campaign` resource), `agents/{planner,worker}.md` |

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
  feat--x.evidence.md              # human evidence (the complete record)
  feat--x.session.json             # adapter-written suspend/resume descriptor
  feat--x.control.json             # cooperative pause flag (reason: user | budget)
  feat--x.budget.json              # adapter-written budget spend (active seconds, cost)
  feat--x.worker_<id>.log          # one log per worker id
  worktrees/                       # the single campaign worktree
  scratch/                         # detached check worktrees (transient)
```

The adapter-written `.session.json` descriptor is the one place a node's status
lives: its `nodes` map records `pending|running|recorded|failed` (and an
`attempts` counter) at suspend time. There is no `state.json`. Git and
`state.db` are authoritative. Only the pi adapter writes the descriptor, the
`.control.json` pause flag, and the `.budget.json` spend file; the engine reads
the descriptor and the flag (`resume`, `sessions`, `ready`), and `gc` removes
the spend file.

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
- `source`: `plane`, `phase:<n>`, or `deliver`, so unrelated verdicts cannot
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

- directory normalization and subtree conflicts, and the DAG phase projection;
- the removed `gpu` field is rejected;
- conformance-by-ownership at phase record time;
- the combined-tree check runner and its cache;
- campaign worktree recording and work-branch selection;
- the campaign confirmation and delivery, and the report;
- the workflow resource grants and field rejection (Node harness);
- end-to-end flows (delivery, idempotency, conflict atomicity, failing checks,
  cleanup, and reporting);
- CLI and packaging smoke tests.

| File | Covers |
|---|---|
| `tests/test_scopes.py` | ownership normalization, `dir`, subtree conflicts |
| `tests/test_phases.py` | DAG phase projection, dependency barriers, caps, the removed `gpu` field, validation |
| `tests/test_merge.py` | the same-directory merge |
| `tests/test_plan.py` | the design's campaign split parse |
| `tests/test_campaign.py` | plane bootstrap and campaign registration, phase recording, report, DAG/state layout |
| `tests/test_campaigns.py` | several campaigns in one plane, scoping, delivery isolation, migration |
| `tests/test_checks.py` | the combined-tree runner, the check cache, sandbox profiles/wrapping, fingerprint invalidation |
| `tests/test_sandbox_gate.py` | manifest discovery/validation, fail-closed gate, the removed `gpu` key, setup |
| `tests/test_phase_scope.py` | campaign worktree reuse, conformance-by-ownership, per-node commits, delivery, default-branch refusal |
| `tests/test_target_branch.py` | the design-derived work branch, the `--feature-branch` override, the base branch, default-branch refusal |
| `tests/test_open_campaign.py` | the worktree from `origin/main`, the local-base fallback, and the stale, ahead, and diverged branch rules |
| `tests/test_store_migration.py` | the `wave` to `phase` migration, the legacy `checks` copy, and the nullable `phase` fallback |
| `tests/test_evidence.py` | the evidence document: commits, checks, diffs, logs, and the bounded Markdown |
| `tests/test_e2e.py` | the full flow from `start` to the delivery pull request |
| `tests/test_pull_request.py` | the `gh` client, the evidence-backed body, the head/base, and the fail-closed rules |
| `tests/test_local_merge.py` | the local delivery mode: the merge commit, the `landed` state, the safety rails, and the idempotent re-run |
| `tests/test_review_approval.py` | the folded campaign approval: the `--confirm` refusal, the one approval row, and the retired comment verbs |
| `tests/test_review_report.py` | the deterministic report and the packet |
| `tests/test_sessions.py` | the descriptor round-trip, the resume plan, cleanup, and the additive `verdicts` table |
| `tests/test_budget.py` | the caps, the pause reason, the `budget` action, and the spend file |
| `tests/test_status_summary.py` | the dense `status` summary |
| `tests/test_cli.py` | CLI surface and lifecycle |
| `tests/test_pi_package.py` | pi package contract, the tool/verbs, the resource, and the agent definitions |
| `tests/campaign_resource_test.mjs` | the resource fixed grants and bounded-field rejection |
| `tests/budget_test.mjs` | the pure budget module: caps, active seconds, cost sum, and the trip test |

## 6. Deliberate gaps

- Symbol/AST extraction is not implemented; directory ownership and
  `git merge-tree` are the detectors. Dependency edges beyond
  `dir`/`depends_on` are not inferred.
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
