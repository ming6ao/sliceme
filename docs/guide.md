# Sliceme guide

Sliceme *(slice the design into parallel agents)* coordinates parallel coding
agents around one campaign. A campaign is a machine-readable DAG, plan-time
**directory ownership**, git worktrees, and fingerprint-pinned delivery as a
pull request against the default branch. This guide covers the model, ownership,
orchestration, and the pi integration. The action reference is in
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
- it checks each recorded wave against a content fingerprint;
- it delivers the campaign as one pull request against the default branch.

The model drafts the plan. The model never decides at run time whether a node
blocks another node.

### Non-goals

- Sliceme does not replace git. Git remains the source of truth.
- Sliceme does not run models. The `sliceme.campaign` workflow resource launches
  the pi-subagents children.
- Sliceme does not resolve arbitrary text conflicts. Local review records one
  campaign approval; it is not a general code-review tool.
- A remote or shared scheduler is out of scope.
- The engine keeps one check runner and one SQLite database per plane.

## 2. Core concepts

| Term | Definition |
|---|---|
| **Campaign** | One campaign branch (the pull request head), one delivery base (the pull request base), and a `dag.json` plan. Several campaigns can share one plane. |
| **Campaign branch** | The feature branch `feat/<name>`. It is the campaign identity and the pull request head. The worktree branch equals it. |
| **Delivery base** | The default branch (`main`). The pull request base. It is recorded once at `start`. |
| **Coordinator** | The top-level pi session that owns the plan and launches the campaign resource. |
| **Node** | One DAG unit of work with `owns`, `depends_on`, `acceptance`, `gpu`. |
| **Unit** | A worktree + branch: the single `campaign` worktree, or a `sliceme/<name>` unit for non-campaign planes. |
| **Ownership** | The repo-relative **directories** a node may change (`dir:` only), compared by subtree overlap. |
| **Conformance** | Check that every changed path lies inside the node's owned directories. |
| **Candidate** | A committed node awaiting delivery. |
| **Wave** | A derived batch of nodes with disjoint owned subtrees that may run concurrently; also the recording order. |
| **Check runner** | The single synchronous combined-tree runner that runs one check set and caches the verdict. |
| **Workflow resource** | `sliceme.campaign`, the trusted loop that pi-subagents runs. |
| **Fingerprint** | Content hash of (tree, command vector, toolchain, policy, sandbox, checks, source) that pins a check result. |
| **Campaign approval** | One decision that covers the whole campaign commit set before delivery. |

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
DAG. It also rejects an empty or blank `owns` entry. A plan that tries to own a
single file, or that leaves a blank entry, fails loudly. A node that changes no
file may omit `owns`, and an empty `owns` list stays valid.

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

One more rule applies to the GPU. A node whose `gpu` field is not `none`
conflicts with **every** other node. A GPU node therefore lands in a wave of its
own, and exactly one GPU node runs at a time.

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

The plan must set `feature_branch`: the campaign branch (the pull request
head). `base` is the delivery base (the pull request base). The coordinator
fixes the campaign branch before the planner runs, so the plan and the engine
agree on one branch.

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
| `depends_on` | Node ids that must reach `done` before this node is `ready`. |
| `acceptance` | Commands the check runner runs for this node. |
| `gpu` | `none`, `T1`, or `T2`; a GPU node runs in a wave of its own. |
| `concurrency` | Per-wave size cap. Defaults to 3 when absent. |

**Phases never schedule; waves do, and Sliceme derives the waves.** A graph plus
the rules

```text
wave(n)  :=  max(wave(d) + 1 for d in n.depends_on), then the earliest wave with
             room (<= concurrency) and no directory-subtree conflict and no GPU node
ready(n) :=  every d in n.depends_on reaches done
```

are the whole scheduler. Readiness is the spawn gate: a node starts once every
dependency reaches `done`, not at a fixed wave. The wave stays a display hint
and caps how many nodes run at once. `done` means **checked and recorded** onto
the campaign worktree. Every wave works in the same worktree, so a later wave
already sees the files of the previous wave without a merge or a rebase. To
batch the pull request until the end therefore does not weaken `depends_on`.

## 5. Orchestration

One top-level **coordinator** turns a design into delivered work. It starts a
planner, then launches the `sliceme.campaign` workflow resource. The resource
runs the loop and the children. The `sliceme` engine stays the deterministic
state, isolation, and delivery layer.

```text
USER
 │  launches pi in the repository
 ▼
COORDINATOR  (top-level pi session — the session the user sees)
 │  tool: sliceme (engine verbs only)
 ├── PLANNER       (sliceme-planner, reads the design, writes dag.json)
 └── sliceme.campaign workflow resource
       ├── WORKER_*  (sliceme-worker, one per ready DAG node, in the campaign worktree)
       │               edit owned dirs only -> stop  (never git, never GPU)
       └── REVIEWER  (builtin reviewer, one per recorded wave)
                       reads the recorded evidence; never edits
```

The coordinator is **not** an `sliceme` unit. The plane starts with
`start --no-unit`, so campaign setup leaves no phantom unit in the coordinator
checkout.

### The workflow resource

The extension registers the trusted resource `sliceme.campaign` in
`session_start` and disposes it in `session_shutdown`. The resource reads the
plane from the workflow `cwd`, so the caller sets `cwd` to the repository root.

The `resolve` function accepts a small set of fields. It rejects every other
field.

| Field | Type | Rule |
|---|---|---|
| `campaign` | string, optional | `[A-Za-z0-9._-]{1,128}` |
| `waveCap` | integer, optional | 1 to 64 |
| `nodeCap` | integer, optional | 1 to 256 |

The `campaign` field is the only variable text in a command. `resolve` validates
the field against the strict pattern before it builds a command.

A host grant binds an exact key and command pair. The `resolve` call cannot know
a node id or a wave index before the campaign runs. Therefore no granted command
carries a node id or a wave index. The engine reads the current wave and the
finished nodes from its own state. `resolve` uses the absolute engine path
captured in `session_start`; a relative path fails, because the workflow `cwd`
is the target repository.

| Key | Command | Use |
|---|---|---|
| `status` | `<python> <engine> --json status <campaign>` | read campaign state, waves, and `paused` |
| `ready` | `<python> <engine> --json ready <campaign>` | get the ready node ids and the current wave |
| `record` | `<python> <engine> --json wave --record --current <campaign>` | commit the finished nodes |
| `check` | `<python> <engine> --json check --current <campaign>` | run the combined-tree checks |

The resource cannot approve a campaign or deliver it. The coordinator records
the approval and runs `deliver` after the user confirms. The user permission is
the only gate.

### Lifecycle

```text
sliceme start <design>      # derive the campaign branch; planner -> dag.json + waves
sliceme wave --open         # fetch main; create the worktree from origin/main
        │
        ▼
resource loop
  1. ready                 current-wave nodes whose dependencies reach done
  2. paused?  -> stop      return the paused state
  3. no ready node? -> stop
  4. runs.run per node     one sliceme-worker per ready node
  5. runs.all              wait for the wave children
  6. record --current      per-node commits on the campaign worktree
  7. check --current       the combined-tree checks (evidence)
  8. runs.run reviewer     one builtin reviewer reads the recorded evidence
  9. caps?    -> stop      waveCap or nodeCap reached
        │  repeat
        ▼
evidence + report ──► user confirms ──► review --decision approve ──► deliver
                                                        (push the campaign branch; pull request against main)
```

1. **`start`** derives the **campaign branch** from the design file name
   (`feat/<slug(design-stem)>`), or takes `--feature-branch`. Sliceme remembers
   the campaign branch and the **delivery base** (the default branch, `main`)
   for the campaign. The campaign branch is never `main`, `master`, or the
   repository default. The planner then writes `dag.json`, and the engine
   projects it into waves.
2. **`ready`** returns the nodes of the current wave whose dependencies all
   reach `done`, plus the wave index and `paused`. Readiness is the gate, so the
   wave is a hint.
3. **The resource** starts one worker per ready node with the `sliceme-worker`
   agent. A worker is a one-shot pure editor in the one shared campaign
   worktree. Sliceme reuses the worktree, so it already contains every earlier
   wave's files.
4. **`wave --record --current`** commits the current wave onto the campaign
   branch, with one commit per node. Sliceme attributes the commit by owned
   directories. Nothing lands on the delivery base.
5. **`check --current`** runs the plane's trusted checks once over the recorded
   wave tree. One synchronous runner writes one terminal row, and the cache
   serves an unchanged fingerprint.
6. **The reviewer** reads the recorded evidence of step 5 and reports findings.
   The reviewer never edits. The loop checks after `record`, because a
   pi-subagents acceptance gate would test the mixed wave worktree.
7. **`evidence`** writes the deterministic evidence document (the commits, the
   checks, the diffs, and the worker logs). The coordinator runs it after the
   resource returns `complete`; the Markdown document is the pull request body.
8. **`review --report`** writes the deterministic skeleton plus the narrative of
   the coordinator. The coordinator runs it before the gate, so the report is
   reviewable. The coordinator shows the evidence summary and the pull request
   details. The details are the head branch, the base `main`, and the title.
   The coordinator asks the user to confirm one time.
9. **`review --decision approve`** records the one campaign approval. The user
   makes the decision, and the coordinator records it with the engine tool.
10. **`deliver`** runs after every wave completes and the user confirms. It runs
    the trusted checks, pushes the campaign branch, and opens one pull request
    against the delivery base (`main`). The campaign branch is never the default
    branch.

The coordinator may invoke the planner again or edit `dag.json` after a
failure. A coordinator-added `depends_on` edge or a widened `owns` changes the
DAG projection, so the next `status` or `ready` reprojects the waves.

### Cleanup

Cleanup is destructive, so the agent cannot choose it without a flag. There is
no per-wave cleanup: Sliceme never removes files between waves. The `deliver`
action takes `--cleanup worktrees`. That option removes the campaign worktree,
drops the campaign branch, and clears scratch. It keeps the report and the
`dag.json` and `state.json` records. `cleanup: all` also removes every other
campaign file: the `dag.json`, `state.json`, `session.json`, and `control.json`
records and the worker logs.

Every cleanup level keeps the report and the evidence document.

`status --gc` removes the files of every campaign that has finished. It keeps
the reports. A closed campaign can hold unmerged work, so `gc` keeps its files
and its branch.

## 6. Checks and sandbox

### 6.1 The single check runner

The engine has one check runner. It runs one check set over a wave tree and
returns the result. It holds no lease and queues no work.

```text
wave --record --current   -> per-node commits on the campaign worktree
check --current           -> one check set over the combined tree
                          -> one terminal row (passed | failed | error) in `checks`
resume / deliver          -> the cache serves an unchanged fingerprint
```

Semantics:

- **One run.** One call runs one check set and writes one terminal row.
- **Dedupe by fingerprint.** A run whose `(tree, commands, toolchain, policy,
  sandbox, checks, source)` fingerprint already has a terminal verdict returns
  the cached row. The runner does not run the commands again.
- **The wave tree.** The checks run over the recorded wave head, so they judge
  the nodes together. Delivery runs the same runner with `source=deliver`.
- **Sandboxed.** Each run resolves the sandbox profile and wraps every command
  (see 6.2).
- **No queue.** There is no `jobs` table, no lease, and no runner process. The
  recorder holds the campaign lock, so recording and checks never interleave.

The `checks` table (`docs/database.md`) records the fingerprint, the wave, the
commit, the status, the results, and the timing.

### 6.2 Sandbox profiles and project manifests

`sliceme/sandbox.py` defines a `Sandbox`: a built-in mode (`none`, `bwrap`,
`unshare`) or a project `command` prefix. The profile also holds the network,
read-only, and writable policy, the `setup` commands, and an optional `gpu`
runner. Sliceme folds `Sandbox.digest()` into the check fingerprint, so a
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
ships no manifest. The check runner resolves the gate before every run. It
refuses to continue on failure.

### 6.3 GPU isolation

The scheduler owns the GPU lane. The `ownership.py` projection makes a GPU node
a conflict with every other node, so a GPU node lands in a wave of its own. One
GPU node runs at a time. There is no separate serialized broker.

`tools/gpu.sh` is the host runner of **Sliceme**: a host `flock`, a
foreign-process gate, and a tiered timeout. Sliceme ships the runner with the
package and invokes it by resolved path, not as `tools/gpu.sh` relative to the
target. The check runner composes the runner outside the project sandbox for
GPU jobs:

```text
check runner -> sliceme gpu runner (host lock) -> project sandbox -> acceptance
```

A project can override the GPU invocation through `gpu.command` in its manifest.

### 6.4 One campaign worktree for all waves

Same-wave nodes own **disjoint directory subtrees** and one GPU node runs alone,
so they cannot author a file collision. All waves share **one campaign
worktree** on a separate branch. The worktree is both the isolation unit and the
accumulation unit:

```text
wave --open                   -> ONE campaign branch feat/<name> + worktree from origin/main
wave --record --wave N        -> conformance-by-ownership -> per-node commits
check --current               -> the one check runner over the combined tree
(no merge per wave)           -> open N+1 in the same worktree
evidence + review --report    -> the deterministic evidence and report
review --decision approve     -> one campaign approval, after the user confirms
deliver                       -> push the campaign branch and open a pull request against main
```

The recorder (`Service.record_wave`) stages the worktree and attributes every
changed path to exactly one same-wave node by `owns`. It rejects an unowned,
ambiguous, or **cross-node rename** change. It then creates one commit and one
prepared candidate per node on the campaign branch. The recorder diffs against
the current `HEAD`, so it never re-attributes a change from an earlier wave.

`record --only <node>` scopes the record to one node. It attributes every
changed path across the whole DAG, ignores a path owned by another node, and
fails only the recorded node. Each failure carries a reason code (`stray_path`,
`ambiguous_path`, `cross_node_rename`, or `missing_description`), so a caller
switches on the code and never parses the message.

The recorder holds the campaign lock, so it serializes with campaign creation
and other records. Sliceme reuses the worktree, so a later wave already sees
every earlier wave's files. Nothing lands on the delivery base until a human
merges the pull request that the single `deliver` step opens.

Workers are pure editors. They never run `git add` or `git commit`, because the
shared index is not safe for multiple processes. They never run the suite in
the shared tree. The check runner snapshots the tree and runs the check set.

### 6.5 Several campaigns in one plane

A plane is one repository root with a `.sliceme/` directory. A plane holds the
one SQLite database, the one delivery lock, and the default branch. Several
campaigns can share the plane at the same time.

Each campaign has its own campaign branch, delivery base, DAG file, state file,
and delivery. The campaigns share one check runner and one database. The
campaign branch identifies a campaign.

Use `--campaign` to name a campaign. The value is a campaign branch, a branch
key, or a unit name. When a plane holds one campaign, `--campaign` is optional.
When a plane holds several campaigns, a call without `--campaign` reports the
plane summary instead of one campaign.

The engine records campaigns in the `campaigns` table. The `config.json` file
keeps a mirror of the newest campaign branch for one release, so an old reader
keeps working.

## 7. Agent integration (pi)

pi is the supported agent harness. The repository is a **pi package**. It
registers one `sliceme` engine tool, the `sliceme.campaign` workflow resource,
and the agent definitions. There is no separate skill.

```bash
pi install ./                                       # local checkout
# pi install git:github.com/ming6ao/sliceme
# pi install npm:sliceme
pi                                                  # launch the coordinator
```

The tool registers **inactive**, so a plain session never advertises it.
`/sliceme [DESIGN.md]` (default `DESIGN.md`) is the single entry point. It
activates the `sliceme` tool for the session and asks the model to start a
campaign. The workflow lives in the prompt guidelines of the tool and in
`docs/workflow.md`. A session binds to a campaign only when the user starts one.

| Role | Bound to a unit? | Contract |
|---|---|---|
| **Coordinator** | no | owns the plan (`dag.json`), starts the resource, records the approval, calls `deliver` once at the end, writes the evidence and the report |
| **Planner** | no | reads the design, writes `dag.json` |
| **Worker** | no (pure editor) | edit owned directories only, then stop; never git, never commit |
| **Reviewer** | no (read-only) | the builtin reviewer judges the recorded wave evidence, never edits |

The extension registers `sliceme-planner` and `sliceme-worker` through the
pi-subagents runtime-agent registry. Each definition carries its `tools:`
allowlist: the worker reads and edits files and runs shell commands, and the
planner reads and writes files. The worker never gets the coordinator verbs. The
resource launches the builtin `reviewer` for the wave review.

### Worker contract

1. You edit the single shared campaign worktree, launched by the resource.
2. Edit only files under the directories your DAG node owns. The wave recorder
   enforces this rule: it rejects a changed path outside the owned directories.
3. Do **not** run `git`, do **not** commit, and do **not** run the test suite.
   Stop after the edits. The engine records the wave and runs the
   combined-tree checks.
4. Never run `deliver`, `git merge`, or `git push`.

### Coordinator

Start a campaign with `/sliceme [DESIGN.md]` in pi. Run `start`, `plan`, and
`wave --open`, then start the `sliceme.campaign` resource with `async: true`.
The resource drives `ready`, `wave --record --current`, and `check --current`
for each wave. When it returns `complete`, run `evidence` and `review --report`,
show the summary and the pull request details to the user, and ask for
confirmation one time. Stop at the human gates: an explicit suspension and the
one confirmation before `deliver`.

## 8. Failure and resume

| Failure | Response |
|---|---|
| Worker produces no candidate / fails the checks | The coordinator retries (bounded), splits the node, or stops. |
| Reviewer reports a problem | The coordinator retries, splits the node, or stops, with the findings attached. |
| A wave record changes a path outside every node's `owns` | The record is rejected; the coordinator widens `owns` or adds a `depends_on` edge and respawns. |
| Merge conflict at `deliver` | The `merge-tree` pre-check refuses delivery; findings surface. Nothing is pushed. |
| Orchestrator crash | The pi-subagents children are children of the coordinator, so a crash kills them. On resume, a node left `running` becomes `paused` if the campaign worktree is present, else `pending`. Sliceme reuses the campaign worktree. Git and `state.db` win over `state.json`. |
| User suspends (`/suspend`) | The pause flag stops new work. The adapter aborts the in-flight turn, which stops the current child within seconds. The interrupted node becomes `paused`, and the adapter writes `.sliceme/<branch-key>.session.json`. Resume (`/campaigns`, `pi --continue`, or `sliceme status --resume`) reconciles from git plus `state.db`: a node interrupted with edits in the shared worktree becomes `paused`, the wave is re-recorded, and unchanged candidates re-verify from the check cache. |

Caps: `concurrency` (the wave size), the resource's `waveCap` and `nodeCap`
fields, and the per-command check timeout bound the cost of each campaign.
