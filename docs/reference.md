# Sliceme reference

Action reference, module map, state layout, verification semantics, tests, and
deliberate gaps. For the model and workflow, see [guide.md](./guide.md).

## 1. Actions

The CLI, the pi `sliceme-unit` tool, and the pi `sliceme` coordinator tool all
derive from one action registry (`sliceme/surface.py`). Seven engine verbs:

| Action | Purpose |
|---|---|
| `start` (alias `init`) | Bootstrap the plane and a unit for the current directory (idempotent). |
| `status` | Units, candidates, waves, health, simulation; `--sessions` and `--resume` cover the campaign registry and resume plan. |
| `deliver` | Merge the campaign worktree into the target feature branch once every commit is approved. |
| `exec` | The single sandboxed executor queue: `submit`/`run`/`wait`/`cancel` check jobs. |
| `wave` | The campaign worktree: `--open` creates or reuses it, `--record --wave N` commits the wave. |
| `review` | Local review: serve the browser client, read a snapshot, poll comments, record a decision, or write the report. |
| `attempt` | Record one subagent attempt's `--begin`/`--end` and its metrics. |

The pi `sliceme` coordinator tool adds orchestration verbs (`ready`, `spawn`,
`record`, `verify`, `deliver`) on top; those drive the engine and the DAG rather
than adding engine actions.

### `start`

```bash
sliceme start [--name N] [--path DIR] [--kind worker]
                [--base REF] [--target BRANCH] [--target-mode current|existing|new]
                [--worktree-branch BRANCH] [--main BRANCH] [--check NAME=COMMAND ...]
                [--force] [--no-unit]
```

Idempotent bootstrap: Sliceme writes `.sliceme/config.json` and
`.sliceme/state.db` when the plane does not exist. It also adds `.sliceme/` to the
repo-local `.git/info/exclude`. It then creates a unit for the directory unless
the directory is already inside one. Re-running from a unit worktree is a
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
sliceme status [--unit U] [--short] [--simulate] [--no-checks] [--health] [--gc]
                 [--sessions] [--resume [--plan-only]]
```

`--short` prints only the current unit name. `--health` checks git/config/db.
`--gc` prunes worktrees, landed-unit branches, and expired review rows.
`--simulate` groups prepared candidates into DAG waves, materializes each
wave's combined tree, and runs the configured checks once over the combined
result; `--no-checks` plans only. `--sessions` lists registered campaigns from
their descriptor files. `--resume` reconciles a suspended campaign from git
plus `state.db` and returns its resume plan; `--plan-only` reports without side
effects.

The default projection returns `dag_waves` (the scheduler's wave plan) plus
per-unit campaign columns (`node`, `log`, `candidate`, `verification`).

### `deliver`

```bash
sliceme deliver [--target BRANCH] [--source BRANCH] [--ff]
                   [--cleanup none|worktrees|all] [--no-checks]
```

- Merges the campaign worktree branch (`--source`, default the recorded
  `worktree_branch`) into the target branch (`--target`, default the recorded
  target) with `git merge --no-ff`.
- Runs the plane's trusted checks on the merged tree; combined checks that fail
  reset the target branch to its pre-merge tip.
- Marks prepared candidates and their unit `landed` without rewriting their
  recorded commits.

It is idempotent: a target that already contains the worktree branch is a
no-op. A merge conflict aborts the merge and returns structured findings
without leaving the target half-merged. Delivery needs the campaign worktree
branch; run `wave --open` first. It refuses until every accumulated commit has
a newest unconsumed `approve` (or an `override` records a note).

**The target is never the repository default branch.** Sliceme refuses `main`,
`master`, and the recorded default, with **no override**. Promotion from a
feature branch to the default branch stays a human `git` step.

### `exec`

```bash
sliceme exec [--submit] [--validate] [--gpu-required] [--run] [--wait] [--cancel]
               [--job ID] [--source SRC] [--commit REF]
               [--command CMD]... [--sandbox none|bwrap|unshare] [--gpu none|T1|T2]
               [--priority N] [--timeout SECONDS] [--wave N]
               [--requester ID] [--limit N]
```

The single serialized executor (``sliceme/executor.py``).  Multiple verifiers
delegate to it instead of each running the acceptance suite.

- `--validate`: resolve and validate the project sandbox gate (manifest and,
  with `--gpu-required`, a GPU runner); exits non-zero when the gate fails.
- `--submit`: enqueue a check job for `--source` (e.g. `node:w1`, `wave:0`),
  `--commit`, and one or more `--command`. A job whose
  `(tree, commands, toolchain, policy, sandbox, source)` fingerprint already
  passed comes back as `cached`; the executor does not run the commands again.
- `--run`: drain the queue with the single runner.  Holds an exclusive `flock`
  on `.sliceme/executor.lock`, so exactly one check vector runs at a time.
- `--wait`: block until `--job` is terminal (or `--timeout`, default 600s).
- `--cancel`: cancel a queued `--job`.
- no flag: print queue counts plus queued/running/recent jobs.

Each job runs in a detached scratch worktree at `--commit`, wrapped in the
resolved sandbox (§4). Sliceme stores the result (status, exit code, output,
duration, fingerprint) in the `jobs` table. `--run` first recovers any `running`
job whose lease expired.

### `wave`

```bash
sliceme wave --open
sliceme wave --record --wave N [--message M] [--summary S]
```

The campaign worktree, split out of `exec` so the executor stays a pure check
queue. Ownership syntax is `dir:PATH`; Sliceme also accepts a bare path. Sliceme rejects
a non-directory spec (`file:`, `symbol:`, …) when it projects the DAG.

- `--open`: create (or reuse) the single **campaign worktree** and branch
  (`worktree_branch`, for example `sliceme/<target-slug>`), idempotently.
  Sliceme uses the same worktree for every wave and never recreates it between
  waves.
- `--record --wave N`: stage the campaign worktree, enforce
  conformance-by-ownership for wave `N`, and create one commit + prepared
  candidate per node. Rejects unowned, ambiguous, or cross-node-rename changes.
  It diffs against the current `HEAD`, so it never re-attributes an earlier
  wave's committed changes. It holds the executor lock, so it serializes with
  check runs.

### `attempt`

```bash
sliceme attempt --begin --node w1 [--unit U] [--attempt N] [--agent worker]
sliceme attempt --end   --node w1 [--attempt N] --status ok [--exit-code 0] \
                        [--turns 7] [--tool-calls 23] [--tokens-in 45210] \
                        [--tokens-out 3120] [--cost 0.42] [--tools '{"bash":6}']
```

Persists one subagent run for one node: a planner, a worker, or a verifier.
The coordinator calls `--begin` before `runSubagent` and `--end` after, and the
same stream feeds a debounced per-node heartbeat file.  `--end` finds the
newest running try for the node.

### `review`

```bash
sliceme review [--serve [--plane DIR ...] [--host H] [--port N]
               [--no-browser] [--url-file PATH]]
               [--state] [--diff --file PATH] [--poll] [--ack --comment-id N]
               [--comment --body TEXT] [--decision approve|request_changes|override]
               [--all] [--commit SHA] [--file PATH] [--side old|new]
               [--line N] [--line-end N] [--note TEXT] [--actor A]
               [--report] [--narrative TEXT] [--design REF]
```

See `docs/review.md` for the local review surface.

- `--serve` runs the foreground loopback server; the client is one static page.
- `--serve` opens the browser when one is available. Use `--no-browser` to
  stop the open. Use `--url-file PATH` to write the URL to a private file.
- `--state` prints one snapshot; `--diff` prints one file diff.
- `--poll` prints the open comments and the approval state.
- `--ack` marks one comment delivered; `--comment` records a comment.
- `--decision` appends a decision for `--commit`, or for every unapproved
  commit with `--all`.
- `--report` writes `.sliceme/<branch-key>.report.md` (a deterministic skeleton
  plus an optional `--narrative`).

The snapshot includes the generated report even though git ignores it. `deliver`
refuses a merge until every accumulated commit has a newest unconsumed
`approve` (or an `override` records a note). The server binds
`127.0.0.1`/`::1` only and requires an `X-Sliceme-Token` write token (carried in
the URL fragment).

### Sandbox manifests

The target repository provides how to run checks in isolation as a tracked
manifest at the repo root: `sliceme.sandbox.json`, `.sliceme-sandbox.json`, or
`tools/sliceme-sandbox.json` (never under `.sliceme/`, which is git-excluded).
Schema: `version`, `command` (prefix receiving `/bin/sh -lc "<cmd>"`),
`network`, `readonly_repo`, `writable`, `setup`, and `gpu`
(`{command, tiers}`).

Resolution: `--sandbox` > `dag.json.sandbox` > `policy.sandbox` > discovered
manifest > `none`.  The planner records `"sandbox": {"path": ...}` in
`dag.json`; the coordinator runs `exec --validate` before verifying and records
the `sandbox_digest` in `state.json` and events.  `policy.require_sandbox` (or
`dag.json.sandbox_required`) fails closed when no profile exists.  A manifest
`digest` recorded in `dag.json` is re-checked, so a post-plan manifest change is
rejected.

## 2. Module map

| Module | Responsibility |
|---|---|
| `sliceme/surface.py` | **single source of truth**: action registry, validation, dispatch |
| `sliceme/cli.py` | generated `argparse` CLI (`sliceme`), human + `--json` output |
| `sliceme/service.py` | **single owner of state**: units, candidates, wave conformance, campaign worktree + recorder, review, and delivery |
| `sliceme/store.py` | SQLite persistence (WAL) |
| `sliceme/gitutil.py` | Git plumbing (`worktree`, `merge`, `merge-tree`, `commit`, `branch`, `changed_files`) |
| `sliceme/ownership.py` | Directory ownership (normalization, `owns`, subtree conflicts) and the DAG wave projection |
| `sliceme/verifier.py` | Fingerprints (plane and node sources) and the sandboxed trusted-check runner |
| `sliceme/sandbox.py` | Isolation profiles + project manifests (`none`/`bwrap`/`unshare`/`command`), the gate, and command wrapping |
| `sliceme/executor.py` | The single sandboxed executor queue (submit/run/wait/cancel, dedupe, leases) |
| `sliceme/integrate.py` | Target selection and guards, final delivery, and combined-tree simulation |
| `sliceme/campaign.py` | `dag.json` / `state.json` layout and readers; deterministic report |
| `sliceme/review/` | Local review: `server.py` (loopback HTTP), `api.py` (action dispatch), `packet.py` (snapshot + commits + report), `diff.py` (diff parsing), `security.py` (token + loopback), `web/` (client) |

The engine is dependency-free Python 3.11+. `Service` is the only state owner;
adapters only parse arguments and render results. `bin/sliceme` is a shim so
the CLI runs without installation.

## 3. State layout

All campaign state lives under `.sliceme/`, **prefixed by the feature-branch
name** so one campaign's files form a single glob and no two campaigns collide.
Let `branch-key` replace `/` with `--` (`feat/x` → `feat--x`):

```text
.sliceme/
  config.json                      # plane config (target_branch, worktree_branch, default_branch, checks, policy)
  state.db                         # units, candidates, jobs, attempts, review_decisions, comments (SQLite, WAL)
  executor.lock                    # exclusive lock held by the single executor runner
  review.lock                      # plane delivery lock (separate from executor.lock)
  feat--x.dag.json                 # canonical plan (never committed)
  feat--x.state.json               # executor progress (node -> status)
  feat--x.report.md                # final report (kept on cleanup)
  feat--x.session.json             # adapter-written suspend/resume descriptor
  feat--x.control.json             # cooperative pause flag
  feat--x.progress_<node>.json     # per-node subagent heartbeat
  feat--x.worker_<id>.log          # one log per worker id
  feat--x.events.jsonl             # audit log (wave replans, verdicts, resume)
  worktrees/                       # the single campaign worktree (+ transient unit worktrees)
  scratch/                         # detached simulation worktrees (transient)
```

`state.json` holds only what git and `state.db` cannot express quickly: per-node
`pending|running|recorded|done|failed|paused`, the last verdict, and the number
of tries. On conflict, git and `state.db` are authoritative; `state.json` is a
rebuildable cache.  Only the pi adapter writes the `.session.json` descriptor;
the engine reads it (`status --resume`, `status --sessions`).

SQLite tables: `units`, `candidates`, `jobs`, `attempts`, `review_decisions`,
`comments`.

## 4. Verification

`fingerprint = sha256(tree, cmd_digest, toolchain_digest, policy_digest, sandbox_digest, executor_digest, source)`:

- `tree`: candidate or combined commit tree;
- `cmd_digest`: the command vector — the plane's configured checks, or a node's
  acceptance commands;
- `toolchain_digest`: `git --version`, Python version, and hashed lockfiles
  (`package-lock.json`, `Cargo.lock`, `go.sum`, `poetry.lock`, …);
- `policy_digest`: policy block of the config;
- `sandbox_digest`: the resolved isolation profile (`sliceme/sandbox.py`), so a
  stricter sandbox invalidates a cached verdict;
- `executor_digest`: the executor semantics version, so changing how checks are
  run invalidates cached verdicts;
- `source`: `plane`, `node:<id>`, or `wave:<n>`, so unrelated verdicts cannot
  collide.

Checks run in a clean detached scratch worktree at the commit and, when a
sandbox exists, wrapped accordingly. The executor serves a passing job
for an unchanged fingerprint from cache; verification never mutates the
candidate or the target branch. The newest terminal job for a commit is the
review evidence. Agent-reported tests are provenance only, never acceptance.

## 5. Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers these areas:

- directory normalization and subtree conflicts, and the DAG wave projection;
- conformance-by-ownership at wave record time;
- the executor queue and the sandbox profiles;
- campaign worktree recording and target-branch selection;
- end-to-end flows (delivery, idempotency, conflict atomicity, failing checks,
  simulation, cleanup, reporting, and per-commit review);
- CLI and packaging smoke tests.

| File | Covers |
|---|---|
| `tests/test_scopes.py` | ownership normalization, `owns`, subtree conflicts |
| `tests/test_waves.py` | DAG wave projection, dependency barriers, caps, validation |
| `tests/test_campaign.py` | plane bootstrap and retargeting, wave recording, report, DAG/state layout |
| `tests/test_executor.py` | executor queue, sandbox profiles/wrapping, fingerprint invalidation |
| `tests/test_sandbox_gate.py` | manifest discovery/validation, fail-closed gate, GPU runner, setup |
| `tests/test_wave_scope.py` | campaign worktree reuse, conformance-by-ownership, per-node commits, delivery, default-branch refusal |
| `tests/test_target_branch.py` | current/existing/new target modes, persistence, default-branch refusal |
| `tests/test_cli.py` | CLI surface and lifecycle |
| `tests/test_pi_package.py` | pi package contract, tool/action lockstep, command gate, docs |
| `tests/test_review.py` | review tables/migration, comment relay, per-commit approvals, the packet/report/diff, and the loopback server |

## 6. Deliberate gaps

- Symbol/AST extraction is not implemented; directory ownership and
  `git merge-tree` are the detectors. Dependency edges beyond `owns`/`depends_on`
  are not inferred.
- No long-lived daemon or unix socket: the CLI calls the SQLite service directly
  (WAL).
- One campaign per plane; RPC-steerable workers and multiple concurrent
  campaigns are out of scope.
- `jj` workspaces and shared dependency caches are not implemented.  Campaign
  workers are pure editors in the single campaign worktree (`wave --open` /
  `wave --record --wave N`).
- Promotion from the target feature branch to the default branch is a human
  `git` step.
