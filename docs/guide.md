# Sliceme guide

Sliceme *(slice the design into parallel agents)* coordinates parallel coding
agents around one campaign. A campaign is a machine-readable DAG, plan-time
**directory ownership**, git worktrees, and fingerprint-pinned delivery onto a
target feature branch. This guide covers the model, ownership, orchestration,
and the pi integration. The action reference is in
[reference.md](./reference.md).

## 1. The problem

Several agents that work on the same repository collide in ways that git cannot
see. The agents edit different files that depend on each other, or they plan
contradictory changes. The failure modes are *authoring conflicts* and
*integration conflicts*.

Git compares *text*, not *intent*. Git lands per branch, not in dependency
order. Sliceme adds a deterministic layer over git:

- it isolates the campaign in one git worktree, on a separate accumulation
  branch;
- it assigns each DAG node disjoint **directories** at plan time;
- it serializes overlapping directory subtrees into waves;
- it verifies each candidate against a content fingerprint;
- it delivers the campaign worktree onto a target feature branch in one
  approved merge.

The model drafts the plan. The model never decides at run time whether a node
blocks another node.

### Non-goals

- Sliceme does not replace git. Git remains the source of truth.
- Sliceme does not run models. The coordinator spawns the client's own headless
  mode.
- Sliceme does not resolve arbitrary text conflicts. The local review client
  is per-commit; it is not a general code-review tool.
- A remote or shared scheduler, or several concurrent campaigns per plane, is
  out of scope.

## 2. Core concepts

| Term | Definition |
|---|---|
| **Campaign** | One target (feature) branch, one campaign worktree branch, a `dag.json` plan, and executor `state.json`. |
| **Coordinator** | The top-level session that owns the plan and drives the campaign. |
| **Node** | One DAG unit of work with `owns`, `depends_on`, `acceptance`, `gpu`. |
| **Unit** | A worktree + branch: the single `campaign` worktree, or a `sliceme/<name>` unit for non-campaign planes. |
| **Ownership** | The repo-relative **directories** a node may change (`dir:` only), compared by subtree overlap. |
| **Conformance** | Check that every changed path lies inside the node's owned directories. |
| **Candidate** | A committed node awaiting verification and delivery. |
| **Wave** | A derived batch of nodes with disjoint owned subtrees that may run concurrently; also the recording order. |
| **Executor** | The single serialized, sandboxed runner that executes check jobs. |
| **Fingerprint** | Content hash of (tree, command vector, toolchain, policy, sandbox, executor, source) that pins a verification result. |

## 3. Directory ownership

Sliceme decides ownership at plan time. There is no runtime declare step, no
lease, and no operation taxonomy.

Every DAG node declares `owns`: repo-relative **directories**. A node must name
the deepest directory that contains each path it will add, modify, or delete.

```jsonc
{ "id": "w1", "owns": ["dir:src/api", "dir:src/api/v1"], "depends_on": [] }
```

Normalization:

| Input | Canonical |
|---|---|
| `dir:src/api` | `src/api` |
| `src/api/` | `src/api` |
| `dir:.`, ``, `/` | `.` (the repository root) |

Sliceme rejects a non-directory spec (`file:`, `symbol:`, `api:`, `schema:`,
`config:`, `migration:`, `infra:`, `test:`) when the coordinator projects the
DAG. A plan that tries to own a single file fails loudly.

### The conflict rule

Ownership is a **subtree**. Two nodes conflict when one owned directory equals
the other, or when one contains the other. Sliceme compares directories on
path-segment boundaries. The root `.` contains every directory, so a node that
owns `dir:.` serializes against every other node.

```
src/api       vs src/api        -> conflict (equal)
src           vs src/api        -> conflict (ancestor)
src/api       vs src/api/v1     -> conflict (descendant)
src/api       vs src/service    -> ok       (siblings)
src/models    vs src/model      -> ok       (no token similarity tier)
```

There is no fuzzy matching. A reader can explain the concurrency from the
`owns` sets alone.

### Conformance: the runtime guarantee

Sliceme enforces the guarantee when the coordinator records the wave:

```text
changed = git diff --name-status HEAD   (in the campaign worktree)
violations = [p for p in changed if p maps to no single wave node]
```

A violation raises an error and Sliceme creates no commit. The coordinator then
widens the `owns` of the node or adds a `depends_on` edge, and respawns. This
rule keeps a wave auditable without runtime locking.

### Authoring guidance

- Own the **deepest** directory that contains the work. A parent directory
  serializes its whole subtree.
- Keep same-wave `owns` disjoint.
- Merge nodes that own the same directory and sit on one dependency chain into
  one node. One node owns one directory and completes the whole cohesive
  change. Set `no_merge` when a node must stay separate for its own gate.
- Route shared build files (`BUILD`, `Cargo.toml`, lockfiles) to an explicit
  **aggregation node**. Every touched part depends on that node, and the node
  owns the shared directory. Use `dir:.` for root files.
- Express ordering that same-directory serialization does not give you with
  `depends_on`. Never rely on a runtime queue.

## 4. The plan: `dag.json` and derived waves

`dag.json` is canonical. There is no `plan.md`. It lives under the
git-excluded `.sliceme/` directory, and Sliceme never commits it.

```jsonc
{
  "campaign": "nanochat-cpp",
  "feature_branch": "feat/nanochat-cpp",
  "base": "master",
  "design": "DESIGN.md",
  "concurrency": 4,
  "nodes": [
    { "id": "w1", "label": "runtime-core", "phase": "P0",
      "goal": "Tensor and the CPU reference backend for all of kernels.h",
      "owns": ["dir:backends/cpu", "dir:src"],
      "depends_on": [],
      "acceptance": ["bazel test //... --test_tag_filters=-gpu"],
      "gpu": "none" }
  ]
}
```

| Field | Meaning |
|---|---|
| `id` | Worker id and unit name; also the log suffix (`worker_<id>.log`). |
| `label` | Human label for the report and dashboard. |
| `phase` | **Display/report grouping only.** Never used for scheduling. |
| `goal` | The prompt seed handed to the worker. |
| `owns` | Directories the node owns, at the deepest subdirectory that contains each path (`dir:src/api`). Directory subtrees are the only conflict unit. |
| `depends_on` | Node ids that must be `done` before this node is `ready`. |
| `acceptance` | Commands the executor runs for this node. |
| `gpu` | `none`, `T1`, or `T2`; only the executor may use it. |
| `concurrency` | Per-wave size cap. Defaults to 3 when absent. |

**Phases never schedule; waves do, and Sliceme derives the waves.** A graph plus
the rules

```text
wave(n)  :=  max(wave(d) + 1 for d in n.depends_on), then the earliest wave with
             room (<= concurrency) and no directory-subtree conflict
ready(n) :=  every d in n.depends_on is done AND n is in the current wave
```

are the whole executor. `done` means **verified and recorded** onto the
campaign worktree. Every wave works in the same worktree, so a later wave
already sees the files of the previous wave without a merge or a rebase. To
batch the merge to the target branch until the end therefore does not weaken
`depends_on`.

## 5. Orchestration

One top-level **coordinator** turns a design into delivered work. It spawns a
planner, workers, and a verifier. The `sliceme` engine stays the deterministic
state, isolation, and delivery layer.

```text
USER
 │  launches pi in the repository
 ▼
COORDINATOR  (top-level pi session — the session the user sees)
 │  tools: sliceme start | status | ready | spawn | record | verify | deliver | report | exec
 ├── PLANNER   (subagent, invoked by the coordinator via `start`)
 │               reads the design, writes dag.json (plane state, not committed)
 ├── WORKER_*  (one-shot subagent per ready DAG node, in the campaign worktree)
 │               edit owned dirs only -> stop  (never git, never GPU)
 └── VERIFIER  (read-only subagent, invoked per candidate)
                 submits checks to the executor; returns a verdict; never edits
```

The coordinator is **not** an `sliceme` unit. The plane starts with
`start --no-unit`, so campaign setup leaves no phantom unit in the coordinator
checkout.

The verifier never runs commands. It delegates to the single sandboxed
**executor** (§6). The executor owns the check queue, the GPU broker, and
isolation.

### Lifecycle

```text
sliceme start <design>      # choose target branch, planner -> dag.json + waves
        │
        ▼
wave N ready ──► spawn (<= concurrency) ──► workers edit the campaign worktree
  ▲                                              │ all workers stop
  │                                              ▼
  │                              record (per-node commits on the worktree)
  │                                              │
  │                                verify (executor runs; verifier judges)
  │                                              │ pass
  │                                              ▼
  │                       node done ◄── mark done (no merge)
  │                                              │
  │            all wave N members done ──► open wave N+1 (same worktree)
  ▼
all nodes done
        │
        ▼
review (approve every commit) ──► deliver ──► merge campaign worktree -> target
```

1. **`start`** asks the user for the **target branch**: the current branch, a
   named existing branch, or a new branch. Sliceme remembers the target for the
   campaign. The target is never `main`, `master`, or the repository default.
   The planner then writes `dag.json`, and the coordinator projects it into
   waves.
2. **`ready`** returns the nodes of the current wave whose dependencies are all
   `done`.
3. **`spawn`** starts a node only in the current wave. It launches a one-shot
   pure editor in the one shared campaign worktree. Sliceme reuses the
   worktree, so it already contains every earlier wave's files.
4. **`record`** commits the current wave onto the campaign worktree, with one
   commit per node. Sliceme attributes the commit by owned directories. Nothing
   merges to the target.
5. **`verify`** runs the read-only verifier on the recorded commit of the node.
   The verifier submits the acceptance vector of the node to the executor (§6).
   The coordinator marks the node `done` from the verdict.
6. **`review`** (`--serve`) opens the local review client. Commits accumulate as
   waves land. The human approves individual commits or all commits at any
   time. The packet also shows the generated report.
7. **`report`** writes the deterministic skeleton plus the narrative of the
   coordinator. The coordinator runs it before delivery, so the report is
   reviewable.
8. **`deliver`** runs after every wave completes and a human approves every
   commit. It merges the campaign worktree into the target branch with the
   trusted checks. The target is never the default branch.

The coordinator may invoke the planner again or edit `dag.json` after a
failure. A coordinator-added `depends_on` edge or a widened `owns` changes the
DAG fingerprint, so the next `status`, `ready`, or `spawn` reprojects the
waves.

### Cleanup

Cleanup is destructive, so the agent cannot choose it without a flag. There is
no per-wave cleanup: Sliceme never removes files between waves. The `deliver`
action takes `--cleanup worktrees`. That option removes the campaign worktree,
drops the already-merged campaign branch, and clears scratch. It keeps the
report and the `dag.json` and `state.json` records. `cleanup: all` also removes
the `dag.json` and `state.json` files and the worker logs, and still keeps the
report.

### GPU arbitration

Only the executor may use the GPU. The T0 CPU remains the inner development
loop. The executor composes the `tools/gpu.sh` broker of Sliceme (a host lock
and a tiered timeout) for GPU jobs. See §6.3. A busy device returns exit 75,
and Sliceme reports a retryable failure rather than a code failure.

### Failure and resume

| Failure | Response |
|---|---|
| Worker produces no candidate / fails acceptance | Node `failed`; coordinator retries (bounded), splits the node, or stops. |
| Verifier `fail` | Same as above, with the verifier's findings attached to the retry prompt. |
| A wave record changes a path outside every node's `owns` | The record is rejected; the coordinator widens `owns` or adds a `depends_on` edge and re-spawns. |
| Merge conflict at `deliver` | Merge aborted; findings surfaced. The target branch is never left half-merged. |
| Orchestrator crash | Workers are child processes of the coordinator and are not detached, so a crash kills them. On resume, a node left `running` becomes `paused` if the campaign worktree is present, else `pending`. Sliceme reuses the campaign worktree. Git and `state.db` win over `state.json`. |
| User suspends (`/suspend`) | The pause flag stops new spawns, records, and verifies. The adapter aborts the in-flight turn, which kills the current worker and any executor subprocess within seconds. The interrupted node becomes `paused`, and the adapter writes `.sliceme/<branch-key>.session.json`. Resume (`/campaigns`, `pi --continue`, or `sliceme status --resume`) reconciles from git plus `state.db`: a node interrupted with edits in the shared worktree becomes `paused`, the wave is re-recorded, and unchanged candidates re-verify from cache. |

Caps: `concurrency`, the maximum number of tries per node, and a wall-clock
budget bound the cost of each spawn.

## 6. Executor, sandbox, and wave scope

### 6.1 The single executor queue

Multiple verifiers never run commands themselves. They submit **check jobs** to
one executor:

```text
VERIFIER A ─┐  submit(job)                    ┌─ wait/notify ─▶ VERIFIER A
VERIFIER B ─┼────────────▶ EXECUTOR QUEUE ────┼─ wait/notify ─▶ VERIFIER B
VERIFIER C ─┘   (SQLite `jobs`, 1 runner)     └─ wait/notify ─▶ VERIFIER C
```

- `sliceme exec --submit --source node:w1 --commit <sha> --command "<cmd>"` enqueues.
- `sliceme exec --run` opens the single-executor lock and drains the queue.
- `sliceme exec --wait --job <id>` blocks until the job is terminal.
- `sliceme exec --cancel --job <id>` cancels a queued job.
- `sliceme exec` with no flags prints the queue status.

Semantics:

- **One runner.** `run` and `drain` hold an exclusive `flock` on
  `.sliceme/executor.lock`. No two check vectors run at the same time.
- **Dedupe by fingerprint.** A submit whose `(tree, commands, toolchain,
  policy, sandbox, source)` fingerprint already passed returns the cached job.
  The executor does not run the commands again.
- **Sandboxed.** Each job carries a `sandbox` profile. The executor resolves the
  profile and wraps every command (see 6.2).
- **Crash-safe.** A `running` job whose lease expired resets to `queued` before
  a drain.

The `jobs` table (`docs/reference.md` §3) records the command vector, the
sandbox digest, the fingerprint, the exit code, the output, and the timing.

### 6.2 Sandbox profiles and project manifests

`sliceme/sandbox.py` defines a `Sandbox`: a built-in mode (`none`, `bwrap`,
`unshare`) or a project `command` prefix. The profile also holds the network,
read-only, and writable policy, the `setup` commands, and an optional `gpu`
runner. Sliceme folds `Sandbox.digest()` into the verification fingerprint, so a
stricter isolation or a changed `setup` invalidates cached verdicts.

Resolution precedence: explicit `--sandbox`, then `dag.json.sandbox`, then
`policy.sandbox`, then a discovered project manifest, then `none`. `none` is
unsandboxed. `policy.require_sandbox` or `dag.json.sandbox_required` makes the
gate fail closed when no profile exists.

The **target repository owns how to run tests in isolation**. It provides a
tracked manifest (`sliceme.sandbox.json`, `.sliceme-sandbox.json`, or
`tools/sliceme-sandbox.json`), never under `.sliceme/`:

```jsonc
{
  "version": 1,
  "command": ["tools/run-in-sandbox.sh", "--"],   // receives /bin/sh -lc "<cmd>"
  "network": false,
  "readonly_repo": true,
  "writable": ["/tmp", ".cache"],
  "setup": ["tools/setup-deps.sh"],               // once per snapshot, before acceptance
  "gpu": { "command": ["sliceme-gpu", "--tier", "{tier}", "--"] }
}
```

The **planner** locates the manifest and records `"sandbox": {"path": ...}` in
`dag.json`, or `"sandbox_required": true` when the project needs isolation but
ships no manifest. The **coordinator** validates the gate with
`sliceme exec --validate` before it spawns or verifies. It records the resolved
`sandbox_digest` in `state.json` and the campaign event log, and it refuses to
continue on failure. Sliceme pins the manifest at plan time. A later manifest
change fails the digest check.

### 6.3 GPU broker ownership

`tools/gpu.sh` is the broker of **Sliceme**: a host `flock`, a foreign-process
gate, and a tiered timeout. Sliceme ships the broker with the package and
invokes it by resolved path, not as `tools/gpu.sh` relative to the target. The
executor composes the broker outside the project sandbox for GPU jobs:

```text
executor → sliceme gpu broker (host lock) → project sandbox → acceptance
```

A project can override the GPU invocation through `gpu.command` in its
manifest.

### 6.4 One campaign worktree for all waves

Same-wave nodes own **disjoint directory subtrees**, so they cannot author a
file collision. All waves share **one campaign worktree** on a separate branch.
The worktree is both the isolation unit and the accumulation unit:

```text
wave --open             -> ONE campaign branch sliceme/<slug> + worktree off the target
wave --record --wave N  -> conformance-by-ownership -> per-node commits (serialized)
verify                  -> the single executor runs each check vector of the node
(no merge per wave)     -> nodes marked done; open N+1 in the same worktree
review --serve          -> the human approves accumulated commits at any time
deliver                 -> merge the campaign worktree once every commit is approved
```

The recorder (`Service.record_wave`) stages the worktree and attributes every
changed path to exactly one same-wave node by `owns`. It rejects an unowned,
ambiguous, or **cross-node rename** change. It then creates one commit and one
prepared candidate per node on the campaign branch. The recorder diffs against
the current `HEAD`, so it never re-attributes a change from an earlier wave.

The recorder holds the executor lock, so it serializes recording with check
runs. Sliceme reuses the worktree, so a later wave already sees every earlier
wave's files. Nothing merges until the single `deliver` step.

Workers are pure editors. They never run `git add` or `git commit`, because the
shared index is not safe for multiple processes. They never run the suite in
the shared tree. The executor snapshots the tree and runs the acceptance
vector. Multiple read-only verifiers judge the recorded evidence of the
executor.

## 7. Agent integration (pi)

pi is the supported agent harness. The repository is a **pi package**. It
registers the `sliceme` coordinator tool, the `sliceme-unit` worker tool, and
the `/sliceme` command. There is no separate skill.

```bash
pi install ./                                       # local checkout
# pi install git:github.com/ming6ao/sliceme
# pi install npm:sliceme
pi                                                  # launch the coordinator
```

Both tools register **inactive**, so a plain session never advertises them.
`/sliceme [DESIGN.md]` (default `DESIGN.md`) is the single entry point. It
activates `sliceme` and `sliceme-unit` for the session and asks the model to
start a campaign. The workflow lives in the prompt guidelines of the tools and
in `docs/workflow.md`. There is no single-agent bootstrap. A session binds to a
unit only when the `spawn` action or the user creates one.

| Role | Bound to a unit? | Contract |
|---|---|---|
| **Coordinator** | no | owns the plan (`dag.json`), spawns agents, records each wave, verifies, calls `deliver` once at the end, writes the report |
| **Planner** | no | reads the design, writes `dag.json` |
| **Worker** | no (pure editor) | edit owned directories only, then stop; never git, never commit |
| **Verifier** | no (read-only) | judges the recorded evidence of the executor, never edits |

`runSubagent` passes the `tools:` allowlist of each agent to `pi --tools`. A
worker gets `sliceme-unit` but never the `sliceme` coordinator tool. The
verifier gets no Sliceme tool at all.

### Worker contract

1. You edit the single shared campaign worktree, launched by `spawn`.
2. Edit only files under the directories your DAG node owns. The wave recorder
   enforces this rule: it rejects a changed path outside the owned directories.
3. Do **not** run `git`, do **not** commit, and do **not** run the test suite.
   Stop after the edits. The coordinator records the wave and the executor runs
   the acceptance vector.
4. Never run `deliver` or `git merge`.

### Coordinator

Start a campaign with `/sliceme [DESIGN.md]` in pi. Then use the `sliceme`
tool, or drive the CLI directly. Run `start --no-unit --target <branch>`.
Then run `spawn`, `record`, and `verify` for each ready wave. Finally, run one
`deliver` after every wave completes and a human approves every accumulated
commit.
