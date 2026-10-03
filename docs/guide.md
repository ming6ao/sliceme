# Sliceme guide

Sliceme *(slice the design into parallel agents)* coordinates parallel coding
agents around one campaign: a machine-readable DAG, plan-time **directory
ownership**, git worktrees, and fingerprint-pinned delivery onto a target
feature branch. This guide covers the model, ownership, orchestration, and the
pi integration; the action reference is in [reference.md](./reference.md).

## 1. The problem

Several agents working the same repository collide in ways Git is blind to: they
edit different files that depend on each other, or plan contradictory changes.
The failure modes are *authoring conflicts* (wasted, contradictory work) and
*integration conflicts* (stale-tip breakage, CI churn).

Git compares *text*, not *intent*, and landing is per-branch rather than ordered
by a dependency graph. Sliceme adds a deterministic layer over Git:

- isolates the campaign in one git worktree (a separate accumulation branch);
- assigns each DAG node disjoint **directories** at plan time;
- serializes overlapping directory subtrees into waves;
- verifies each candidate against a content fingerprint;
- delivers the campaign worktree onto a target feature branch in one approved
  merge when every wave is done.

LLMs draft the plan; they never decide at runtime whether something blocks.

### Non-goals

- Replacing Git. Git remains the source of truth.
- Running models. The coordinator spawns the client's own headless mode.
- Resolving arbitrary text conflicts or providing a review UI.
- A remote/shared scheduler or multiple concurrent campaigns per plane.

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

Ownership is decided entirely at plan time. There is no runtime declare step, no
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

Non-directory specs — `file:`, `symbol:`, `api:`, `schema:`, `config:`,
`migration:`, `infra:`, `test:` — are rejected when the DAG is projected. A plan
that tries to own a single file fails loudly rather than silently receiving
directory-level serialization.

### The conflict rule

Ownership is a **subtree**. Two nodes conflict when one owned directory is equal
to, an ancestor of, or a descendant of the other's, compared on path-segment
boundaries. The root `.` is an ancestor of every directory, so a node that owns
`dir:.` serializes against every other node.

```
src/api       vs src/api        -> conflict (equal)
src           vs src/api        -> conflict (ancestor)
src/api       vs src/api/v1     -> conflict (descendant)
src/api       vs src/service    -> ok       (siblings)
src/models    vs src/model      -> ok       (no token similarity tier)
```

There is deliberately no fuzzy matching: concurrency is explainable from the
`owns` sets alone.

### Conformance: the runtime guarantee

Because there are no leases, the guarantee is enforced after the worker commits:

```text
changed = git diff --name-only <unit.base_commit> <head>
violations = [p for p in changed if p not in the subtree of any owned dir]
```

Any violation raises an error and the candidate is not registered. The
coordinator then widens the node's `owns` or adds a `depends_on` edge and
re-spawns. This keeps "no two same-wave units touch the same directory"
auditable without runtime locking.

### Authoring guidance

- Own the **deepest** directory that contains the work; owning a parent
  serializes its whole subtree.
- Keep same-wave `owns` disjoint.
- Route shared build files (`BUILD`, `Cargo.toml`, lockfiles) to an explicit
  **aggregation node** that every touched component `depends_on`; that node owns
  the shared directory (`dir:.` for root files).
- Express ordering that same-directory serialization does not already give you
  with `depends_on`, never by hoping for a runtime queue.

## 4. The plan: `dag.json` and derived waves

`dag.json` is canonical; there is no `plan.md`. It lives under the git-excluded
`.sliceme/` directory and is never committed.

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

**Phases never schedule; waves do — and waves are derived.** A graph plus the
rules

```text
wave(n)  :=  max(wave(d) + 1 for d in n.depends_on), then earliest wave with
             room (<= concurrency) and no directory-subtree conflict
ready(n) :=  every d in n.depends_on is done AND n is in the current wave
```

are the whole executor. `done` means **verified *and* recorded** onto the
campaign worktree. Because every wave works in the same worktree, a later wave
already sees the previous wave's files without any merge or rebase, so batching
the merge to the target branch until the end does not weaken `depends_on`.

## 5. Orchestration

One top-level **coordinator** turns a design into delivered work by spawning a
planner, workers, and a verifier, while the `sliceme` engine remains the
deterministic state, isolation, and delivery layer.

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

The coordinator is **not** an `sliceme` unit: plane bootstrap uses
`start --no-unit`, so campaign setup leaves no phantom unit in the coordinator's
checkout.

The verifier never runs commands itself: it delegates to the single sandboxed
**executor** (§6), which owns the check queue, the GPU broker, and isolation.

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
deliver (ask approval once) ──► merge campaign worktree -> target ──► report
```

1. **`start`** asks the user for the **target branch** — the current branch, a
   named existing branch, or a new branch.  The target is remembered for the
   campaign and is never `main`, `master`, or the repository default.  The
   planner then writes `dag.json`; the coordinator projects it into waves.
2. **`ready`** returns the current wave's nodes whose dependencies are all
   `done`.
3. **`spawn`** only starts a node in the current wave. It launches a one-shot
   pure editor in the one shared campaign worktree; the worktree is reused, so
   it already contains every earlier wave's files.
4. **`record`** commits the current wave onto the campaign worktree: one commit
   per node, attributed by owned directories.  Nothing is merged to the target.
5. **`verify`** runs the read-only verifier on the node's recorded commit.  The
   verifier submits the node's acceptance vector to the executor (§6) and the
   coordinator marks the node `done` from the verdict.
6. **`deliver`** (only when every wave is done) asks the user once for approval,
   then merges the campaign worktree into the target branch with the trusted
   checks.  The target is never the default branch.
7. **`report`** writes the deterministic skeleton plus the coordinator's
   narrative.

The coordinator may re-invoke the planner or edit `dag.json` after a failure. A
coordinator-added `depends_on` edge (or a widened `owns`) changes the DAG
fingerprint, so the next `status`/`ready`/`spawn` reprojects the waves.

### Cleanup

Cleanup is destructive, so the agent cannot silently choose it, and there is no
per-wave cleanup: files are never removed between waves.  As part of the
end-of-campaign delivery approval, the coordinator offers one campaign-wide
cleanup.  Accepting it runs `deliver --cleanup worktrees`, which removes the
campaign worktree, drops the already-merged campaign branch, and clears scratch;
the report and the `dag.json`/`state.json` record are kept.  `cleanup: all`
additionally removes the `dag.json`/`state.json` files and the worker logs (the
report is still kept).

### GPU arbitration

Only the executor may use the GPU; T0 CPU remains the inner development loop.
The executor composes sliceme's `tools/gpu.sh` broker (host lock + tiered
timeout) for GPU jobs — see §6.3. A busy device returns exit 75, reported as a
retryable failure rather than a code failure.

### Failure and resume

| Failure | Response |
|---|---|
| Worker produces no candidate / fails acceptance | Node `failed`; coordinator retries (bounded), splits the node, or stops. |
| Verifier `fail` | Same as above, with the verifier's findings attached to the retry prompt. |
| A wave record changes a path outside every node's `owns` | The record is rejected; the coordinator widens `owns` or adds a `depends_on` edge and re-spawns. |
| Merge conflict at `deliver` | Merge aborted; findings surfaced. The target branch is never left half-merged. |
| Orchestrator crash | Workers are child processes of the coordinator and are not detached, so a crash kills them. On resume, any node left `running` is reset to `pending` and re-spawned; the campaign worktree is reused, not recreated. Git and `state.db` win over `state.json`. |
| User suspends (`/suspend`) | The pause flag stops new spawns/records/verifies and the adapter aborts the in-flight turn, killing the current worker and any executor subprocess within seconds; the interrupted node is marked `paused` and the adapter writes `.sliceme/<branch-key>.session.json`. Resume (`/campaigns`, `pi --continue`, or `sliceme resume`) reconciles from git plus `state.db`: a node interrupted with edits in the shared worktree becomes `paused`, the wave is re-recorded, and unchanged candidates re-verify from cache. |

Caps: `concurrency`, max attempts per node, and a wall-clock budget bound the
cost of each spawn.

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

- **One runner.** `run`/`drain` hold an exclusive `flock` on
  `.sliceme/executor.lock`, so no two check vectors run concurrently.
- **Dedupe by fingerprint.** A submit whose `(tree, commands, toolchain, policy,
  sandbox, source)` fingerprint already passed returns the cached job; the
  commands are not re-run.
- **Sandboxed.** Each job carries a `sandbox` profile; the executor resolves it
  and every command is wrapped (see 6.2).
- **Crash-safe.** A `running` job whose lease expired is reset to `queued`
  before a drain.

Jobs are recorded in the `jobs` table (`docs/reference.md` §3) with their
command vector, sandbox digest, fingerprint, exit code, output, and timing.

### 6.2 Sandbox profiles and project manifests

`sliceme/sandbox.py` defines a `Sandbox`: a built-in mode (`none`, `bwrap`,
`unshare`) or a project `command` prefix, plus network/read-only/writable
policy, `setup` commands, and an optional `gpu` runner. `Sandbox.digest()` is
folded into the verification fingerprint, so tightening isolation or changing
`setup` invalidates cached verdicts.

Resolution precedence: explicit `--sandbox` > `dag.json.sandbox` >
`policy.sandbox` > discovered project manifest > `none`. `none` is unsandboxed;
`policy.require_sandbox` (or `dag.json.sandbox_required`) makes the gate fail
closed when no profile exists.

The **target repository owns how to run tests in isolation** through a tracked
manifest (`sliceme.sandbox.json`, `.sliceme-sandbox.json`, or
`tools/sliceme-sandbox.json` -- **not** under `.sliceme/`, which is git-excluded):

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
`dag.json` (or `"sandbox_required": true` when the project needs isolation but
ships no manifest).  The **coordinator** validates the gate with
`sliceme exec --validate` before spawning or verifying, records the resolved
`sandbox_digest` in `state.json` and the campaign event log, and refuses to
continue on failure -- so the sandbox is present before any verifier runs.  A
manifest that changes after the plan is pinned is rejected by digest.

### 6.3 GPU broker ownership

`tools/gpu.sh` is **sliceme's** broker: a host `flock`, a foreign-process gate,
and a tiered timeout. It is shipped with the package and invoked by resolved
path, not as `tools/gpu.sh` relative to the target. The executor composes it
outside the project sandbox for GPU jobs:

```text
executor → sliceme gpu broker (host lock) → project sandbox → acceptance
```

A project may override the GPU invocation through `gpu.command` in its manifest.

### 6.4 One worktree per wave

Because same-wave nodes own **disjoint directory subtrees**, they cannot author
a file collision.  All waves share **one campaign worktree** on a separate
branch, so the worktree is both the isolation and the accumulation unit:

```text
exec --open             -> ONE campaign branch sliceme/<slug> + worktree off the target
exec --record --wave N  -> conformance-by-ownership -> per-node commits (serialized)
verify                  -> the single executor runs each node's check vector
(no merge per wave)     -> nodes marked done; open N+1 in the same worktree
deliver                 -> after approval, merge the campaign worktree into the target
```

The recorder (`Service.record_wave`) stages the worktree, attributes every
changed path to exactly one same-wave node by `owns`, rejects an unowned,
ambiguous, or **cross-node rename** change, then creates one commit and one
prepared candidate per node on the campaign branch.  It diffs against the
current `HEAD`, so an earlier wave's committed changes are never re-attributed.
It holds the executor lock, so recording is serialized with check runs.
Because the worktree is reused, a later wave already sees every earlier wave's
files; nothing is merged until the single `deliver` step.

Workers are pure editors: they never run `git add/commit` (the shared index is
not multi-process safe) and never run the suite in the shared tree; the executor
snapshots the tree and runs the acceptance vector.  Multiple read-only verifiers
judge the executor's recorded evidence.

## 7. Agent integration (pi)

pi is the supported agent harness. The repository is a **pi package** that ships
one extension registering the `sliceme` coordinator tool, the `sliceme-unit`
worker tool, and the `/sliceme` command. There is no separate skill.

```bash
pi install ./                                       # local checkout
# pi install git:github.com/ming6ao/sliceme
# pi install npm:sliceme
pi                                                  # launch the coordinator
```

Both tools register **inactive**, so a plain session never advertises them.
`/sliceme [DESIGN.md]` (default `DESIGN.md`) is the single entry point: it
activates `sliceme` and `sliceme-unit` for the session and asks the model to
start a campaign. The workflow is condensed into their prompt guidelines and
documented in [docs/workflow.md](./workflow.md). There is no single-agent
bootstrap — a session is only bound to a unit when the `spawn` action (or the
user) creates one.

| Role | Bound to a unit? | Contract |
|---|---|---|
| **Coordinator** | no | owns the plan (`dag.json`), spawns agents, records each wave, verifies, calls `deliver` once at the end, writes the report |
| **Planner** | no | reads the design, writes `dag.json` |
| **Worker** | no (pure editor) | edit owned directories only, then stop; never git, never commit |
| **Verifier** | no (read-only) | judges the executor's recorded evidence, never edits |

`runSubagent` passes each agent's `tools:` allowlist to `pi --tools`, so a
worker gets `sliceme-unit` but never the `sliceme` coordinator tool, and the
verifier gets no Sliceme tool at all.

### Worker contract

1. You edit the single shared campaign worktree, launched by `spawn`.
2. Edit only files under the directories your DAG node owns. The wave recorder
   enforces this: a changed path outside the owned directories is rejected.
3. Do **not** run `git`, do **not** commit, and do **not** run the test suite.
   Stop after editing; the coordinator records the wave and the executor runs
   the acceptance vector.
4. Never run `deliver` or `git merge`.

### Coordinator

Start a campaign with `/sliceme [DESIGN.md]` in pi, then use the
`sliceme` tool — or drive the CLI directly: `start --no-unit --target <branch>`,
then `spawn`/`record`/`verify` per ready wave, and a single `deliver` once every
wave is done and the user approves.
