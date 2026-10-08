# Sliceme reference

Action reference, module map, state layout, verification semantics, tests, and
deliberate gaps. For the model and workflow, see [guide.md](./guide.md).

## 1. Actions

The CLI and the pi `sliceme` tool derive from one action registry
(`sliceme/surface.py`). The engine has eight verbs:

| Action | Purpose |
|---|---|
| `start` (alias `init`) | Bootstrap the plane and a campaign for the current directory (idempotent). |
| `status` | Units, candidates, waves, checks, health, simulation; the default is the dense summary, `--verbose` is the full dump, and `--sessions`/`--resume` cover the campaign registry and the resume plan. |
| `ready` | The current-wave node ids, the wave index, and `paused`. |
| `plan` | Parse the design's campaign split and join the registry state. |
| `deliver` | Push the campaign worktree branch and open the delivery pull request after approval. |
| `check` | Run the synchronous combined-tree checks for the current wave (`--current`). |
| `wave` | The campaign worktree: `--open` creates or reuses it, `--record` commits a wave as per-node commits. |
| `review` | Record one campaign decision, or write the deterministic report. |

The pi extension forwards these verbs through one tool. The campaign loop lives
in the `sliceme.campaign` workflow resource, not in the engine.

Most campaign-scoped actions accept `--campaign REF`. The reference is a target
branch, a branch key, or a unit name. When a plane holds one campaign,
`--campaign` is optional. When a plane holds several campaigns, a
campaign-scoped call without `--campaign` returns the plane summary. See §3 for
the layout.

### `start`

```bash
sliceme start [--name N] [--path DIR] [--kind worker]
                [--base REF] [--target BRANCH] [--target-mode current|existing|new]
                [--worktree-branch BRANCH] [--main BRANCH] [--check NAME=COMMAND ...]
                [--force] [--no-unit] [--campaign REF]
```

Idempotent bootstrap: Sliceme writes `.sliceme/config.json` and
`.sliceme/state.db` when the plane does not exist. It also adds `.sliceme/` to
the repo-local `.git/info/exclude`. It then creates a unit for the directory
unless the directory is already inside one. Re-running from a unit worktree is a
no-op.

- `--target BRANCH` with `--target-mode current|existing|new` chooses the
  campaign's **target (feature) branch** once and records it. `current` adopts
  the checked-out branch, `existing` requires the named branch, and `new`
  creates it from `--base`.
- The target is **never** `main`, `master`, or the repository default branch;
  `deliver` refuses it. There is no override.
- `--worktree-branch BRANCH` names the separate campaign accumulation branch
  (default derived, for example `sliceme/<target-slug>`).
- `--main BRANCH` is a deprecated alias for `--target`.
- `--base REF` records the fork point (default: the target branch).
- `--no-unit` initialises the plane without creating a unit, for a coordinator's
  checkout.
- `--check NAME=COMMAND` registers a trusted plane check (repeatable).
- Sliceme captures the recorded `default_branch` once at init: origin `HEAD`,
  else `init.defaultBranch`, else an existing `main` or `master`, else `main`.

The programmatic plane-only helper is `Service.init_plane(root, ...)`.

### `status`

```bash
sliceme status [--unit U] [--short] [--dense] [--verbose] [--simulate] [--no-checks]
                 [--health] [--gc] [--sessions] [--resume [--plan-only]] [--campaign REF]
```

The default human output is the **dense summary**. It has a header, one line per
node in DAG order, and one line per wave. The header shows `campaign`,
`target`/`worktree`/`base`, `design`, node count, and wave size. A node line
shows `w<wave> <id> [<phase>] <status>` plus the node label. A wave line shows
the index, the status, and the member ids.

`--dense` asks for it explicitly and `--verbose` prints the full nested dump
instead. With `--json` the default is the nested dump; `--dense --json` emits
the summary as JSON (including `lines`). `--short` prints only the current unit
name. `--health` checks git/config/db.

`--gc` prunes worktrees, landed-unit branches, scratch, expired review rows, and
the files of every campaign that has finished. It keeps the report.
`--simulate` groups prepared candidates into DAG waves, materializes each
wave's combined tree, and runs the configured checks once over the combined
result; `--no-checks` plans only. `--sessions` lists registered campaigns from
their descriptor files. `--resume` reconciles a suspended campaign from git plus
`state.db` and returns its resume plan; `--plan-only` is a compatibility no-op,
because the resume plan is a pure read.

The nested dump (`--verbose`, or `--json` without `--dense`) returns
`dag_waves` (the scheduler's wave plan), `ready` (the ready node ids), `paused`,
`checks` (check counts), and per-unit campaign columns (`node`, `log`,
`candidate`, `verification`). The dense summary returns the header and the
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
candidate or a status other than `pending`. Sliceme reports the result as
`dag_merge` (`merged`, `before_nodes`, `after_nodes`, `before_waves`,
`after_waves`).

### `ready`

```bash
sliceme ready [--campaign REF]
```

Returns `campaign`, `ready` (the current-wave node ids whose dependencies are
`done`), `wave` (the current wave index), and `paused`. The workflow resource
polls this verb, so its fields are the loop contract. A node is ready when every
`depends_on` dependency is `done` and the node itself is neither `done` nor
`running`. Sliceme scopes the result to the current wave, so a caller spawns and
records one wave per call. A missing DAG reports no ready nodes.

### `plan`

```bash
sliceme plan --design DESIGN.md
```

Read the `sliceme-campaigns` fenced block from the design document. Join the
entries, in order, with the campaign registry state. Print each entry's name,
target, base, directory scope, and state. Print `next`: the first entry whose
campaign is not delivered, landed, or closed.

Use the plan to run one design as a sequence of campaigns. Each campaign has its
own DAG and worktree, so a directory may repeat across entries. The engine keeps
one owner per directory inside one campaign. See
[multi-campaign.md](./multi-campaign.md) section 15.

### `deliver`

```bash
sliceme deliver [--target BRANCH] [--source BRANCH]
                   [--cleanup none|worktrees|all] [--no-checks] [--campaign REF]
```

- Pushes the campaign worktree branch (`--source`, default the recorded
  `worktree_branch`) to the remote (`policy.remote`, default `origin`).
- Opens one pull request against the target branch (`--target`, default the
  recorded target) with the `gh` program. The body is the campaign report.
- Pre-checks the merge with `git merge-tree`. A conflict returns structured
  findings before the push.
- Runs the plane's trusted checks on the campaign head through the single check
  runner; a failed check stops the delivery before the push. The runner caches
  the verdict by fingerprint.
- Marks prepared candidates and their unit `landed` without rewriting their
  recorded commits.

It is idempotent: Sliceme returns an open pull request as it is, and a target
that already contains the worktree branch is a no-op. Delivery needs the
campaign worktree branch; run `wave --open` first. It refuses until the newest
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

**The target is never the repository default branch.** Sliceme refuses `main`,
`master`, and the recorded default, with **no override**. Promotion from a
feature branch to the default branch stays a human act on the forge.

### `check`

```bash
sliceme check --current [--campaign REF]
```

Run the plane's trusted checks once over the current wave's combined tree. The
wave tree is the campaign worktree head after `wave --record`, so the checks run
over the recorded nodes together. The verb reports `wave`, `members`, and the
row (`fingerprint`, `status`, `duration`, `exit_code`, `output`, `results`,
`cached`). The cache serves a fingerprint that already has a terminal verdict,
so a resumed node reads the cache. The verb requires `--current`; the engine
reads the wave index from its own state.

### `wave`

```bash
sliceme wave --open
sliceme wave --record --current [--only NODE]... [--messages JSON] [--summary S]
sliceme wave --record --wave N [--only NODE]... [--messages JSON] [--summary S]
```

The campaign worktree and recorder. Ownership syntax is `dir:PATH`; Sliceme also
accepts a bare path. Sliceme rejects a non-directory spec (`file:`, `symbol:`,
…) and an empty or blank entry when it projects the DAG. A node that changes no
file may omit `owns`.

- `--open`: create (or reuse) the single **campaign worktree** and branch
  (`worktree_branch`, for example `sliceme/<target-slug>`), idempotently. Sliceme
  uses the same worktree for every wave and never recreates it between waves.
- `--record --current`: record the wave whose index the engine holds. The
  `--current` flag is the workflow-resource form, because a host grant cannot
  know a wave index in advance.
- `--record --wave N`: stage the campaign worktree, enforce
  conformance-by-ownership for wave `N`, and create one commit + prepared
  candidate per node. Sliceme rejects unowned, ambiguous, or cross-node-rename
  changes. It diffs against the current `HEAD`, so it never re-attributes an
  earlier wave's committed changes.
- `--only NODE`: scope the record to one node (repeatable). Sliceme attributes
  every changed path across the whole DAG, ignores a path owned by another node,
  and fails only the recorded node. Each failure carries a reason code
  (`stray_path`, `ambiguous_path`, `cross_node_rename`, or
  `missing_description`), so a caller switches on the code, never the text.
- The commit subject is the node's human description. Sliceme uses the
  per-node `--messages '{"w1": "..."}'` entry. A node with changes and no
  description is an error. The subject never carries a wave prefix. The node and
  the wave stay in `state.db` and the DAG.

The recorder holds the campaign lock, so it serializes with campaign creation
and other records. Checks are synchronous and run inside the caller, so they
need no lock.

### `review`

```bash
sliceme review [--decision approve|request_changes|override] [--all]
                 [--commit SHA] [--note TEXT] [--actor A]
                 [--report] [--narrative TEXT] [--design REF]
```

See `docs/review.md` for the approval gate and the report.

- `--decision` appends one **campaign-level** decision (`commit_hash` is null).
  `approve` covers the whole accumulated commit set. `request_changes` needs a
  note. `override` is a campaign-level decision that needs a note.
- The `--all` and `--commit` flags stay for compatibility. One approval covers
  the campaign, so the flags do not change the stored decision.
- `--report` writes `.sliceme/<branch-key>.report.md`: a deterministic skeleton
  plus an optional `--narrative`.

Delivery proceeds only while the newest campaign decision is an unconsumed
`approve`, or an `override` that records a note. A successful delivery consumes
the decision it used. A failed check or forge call leaves the decision
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
| `sliceme/verbs/` | the verb groups: `bootstrap`, `campaign`, `status`, `review`, `delivery`, `sessions`, `support` |
| `sliceme/store.py` | SQLite persistence (WAL) |
| `sliceme/gitutil.py` | Git plumbing (`worktree`, `merge`, `merge-tree`, `commit`, `branch`, `push`, `changed_files`) |
| `sliceme/ownership.py` | Directory ownership (normalization, `owns`, subtree conflicts), per-node `readiness`, the DAG wave projection, GPU isolation, and the same-ownership merge |
| `sliceme/plan.py` | the design's `sliceme-campaigns` split: parse and validate |
| `sliceme/verifier.py` | Fingerprints and the sandboxed check runner |
| `sliceme/sandbox.py` | Isolation profiles + project manifests (`none`/`bwrap`/`unshare`/`command`), the gate, and command wrapping |
| `sliceme/checks.py` | The single synchronous combined-tree check runner plus the `checks` cache |
| `sliceme/integrate.py` | Target selection and guards, pull request delivery, and combined-tree simulation |
| `sliceme/pullrequest.py` | The `gh` forge client: find or create the delivery pull request |
| `sliceme/campaign.py` | `dag.json` / `state.json` layout and readers; deterministic report |
| `sliceme/review/` | Reduced review: `api.py` (decision + report dispatch), `packet.py` (snapshot + commits + report + evidence), `diff.py` (diff parsing) |
| `integrations/pi/` | the pi adapter: `common.ts` (invocation and paths), `coordinator.ts` (tool, agents, commands), `campaign-resource.ts` (the `sliceme.campaign` resource), `agents/{planner,worker}.md` |

The engine is dependency-free Python 3.11+. `Service` is the only state owner;
adapters only parse arguments and render results. `bin/sliceme` is a shim so the
CLI runs without installation.

## 3. State layout

All campaign state lives under `.sliceme/`, **prefixed by the target-branch
name** so one campaign's files form a single glob and no two campaigns collide.
Let `branch-key` replace `/` with `--` (`feat/x` → `feat--x`):

```text
.sliceme/
  config.json                      # plane config (default_branch, checks, policy)
  state.db                         # campaigns, units, candidates, checks, review_decisions (SQLite, WAL)
  campaigns.lock                   # campaign-creation and wave-record lock
  review.lock                      # plane delivery lock
  active.<pid>.campaign            # per-process pointer to the session's campaign
  feat--x.dag.json                 # canonical plan (never committed)
  feat--x.state.json               # optional read-only legacy override (never written by the engine)
  feat--x.report.md                # final report (kept on cleanup)
  feat--x.session.json             # adapter-written suspend/resume descriptor
  feat--x.control.json             # cooperative pause flag
  feat--x.worker_<id>.log          # one log per worker id
  worktrees/                       # the single campaign worktree
  scratch/                         # detached simulation and check worktrees (transient)
```

`state.json` is an optional, read-only legacy override that the engine never
writes. It holds per-node `pending|running|recorded|done|failed|paused`, the
current wave, the wave list, and the number of tries. On conflict, git and
`state.db` are authoritative. Only the pi adapter writes the `.session.json`
descriptor and the `.control.json` pause flag; the engine reads them
(`status --resume`, `status --sessions`, `ready`).

SQLite tables: `campaigns`, `units`, `candidates`, `checks`, `review_decisions`.

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
campaign re-verifies from the cache. Verification never mutates the candidate or
the target branch. The newest terminal check for a commit is the review
evidence. Agent-reported tests are provenance only, never acceptance.

The runner is **synchronous**. One call runs one check set and writes one
terminal row. There is no queue, no lease, and no second runner. The
`_dispatch_wave` recorder holds the campaign lock, so recording and checks never
interleave.

## 5. Tests

```bash
npm test
```

The suite covers these areas:

- directory normalization and subtree conflicts, and the DAG wave projection;
- GPU isolation: two GPU nodes land in two waves;
- conformance-by-ownership at wave record time;
- the combined-tree check runner and its cache;
- campaign worktree recording and target-branch selection;
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
| `tests/test_campaign.py` | plane bootstrap and retargeting, wave recording, report, DAG/state layout |
| `tests/test_campaigns.py` | several campaigns in one plane, scoping, delivery isolation, migration |
| `tests/test_checks.py` | the combined-tree runner, the check cache, sandbox profiles/wrapping, fingerprint invalidation |
| `tests/test_sandbox_gate.py` | manifest discovery/validation, fail-closed gate, GPU runner, setup |
| `tests/test_wave_scope.py` | campaign worktree reuse, conformance-by-ownership, per-node commits, delivery, default-branch refusal |
| `tests/test_target_branch.py` | current/existing/new target modes, persistence, default-branch refusal |
| `tests/test_pull_request.py` | the `gh` client, the report-backed body, and the fail-closed rules |
| `tests/test_review_approval.py` | the one campaign approval, the override, and the retired comment verbs |
| `tests/test_review_report.py` | the deterministic report and the packet |
| `tests/test_sessions.py` | the descriptor round-trip, the resume plan, cleanup, and the additive `checks` table |
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
- One campaign occupies one target branch; several campaigns can share a plane.
  RPC-steerable workers are out of scope.
- `jj` workspaces and shared dependency caches are not implemented. Campaign
  workers are pure editors in the single campaign worktree (`wave --open` /
  `wave --record`).
- Promotion from the target feature branch to the default branch is a human
  `git` step.
- Child status, events, and control belong to pi-subagents. Sliceme reads the
  engine's own state and does not keep a per-child progress file.
