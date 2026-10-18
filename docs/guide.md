# Sliceme guide

> **Status.** This document describes the directory-slice design: one node per
> directory. The engine change is tracked in
> [directory-slices-plan.md](./directory-slices-plan.md). The GPU field is
> removed.

Sliceme *(slice the design into parallel agents)* coordinates parallel coding
agents around one campaign. A campaign is a machine-readable DAG, plan-time
**directory ownership**, git worktrees, and fingerprint-pinned delivery. A
delivery is one pull request, or one local merge into the local base branch.
This guide covers the model, ownership, orchestration, and the pi integration. The action reference is in
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
- it serializes overlapping directory subtrees into phases;
- it checks each recorded phase against a content fingerprint;
- it delivers the campaign as one pull request, or as one local merge into the
  local base branch.

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
| **Campaign** | One work branch (the pull request head), one base branch (the pull request base), and a `dag.json` plan. Several campaigns can share one plane. |
| **Work branch** | The branch `feat/<name>`. It is the campaign identity and the pull request head. |
| **Base branch** | The branch the pull request lands on (the default branch, `main`). It is recorded once at `start`. |
| **Coordinator** | The top-level pi session that owns the plan and launches the campaign resource. |
| **Slice** | One node, one directory. The slice is the scheduling unit, the worker unit, and the commit unit. |
| **Node** | One directory slice with `dir`, `tasks`, `depends_on`, and `acceptance`. |
| **Unit** | *(retired)* The campaign owns the one worktree and branch. |
| **Ownership** | The repo-relative **directories** a node may change, compared by subtree overlap. |
| **Conformance** | Check that every changed path lies inside the node's owned directories. |
| **Record** | A committed node awaiting delivery. |
| **Phase** | A derived batch of nodes with disjoint owned subtrees that may run concurrently; also the recording order. |
| **Check runner** | The single synchronous combined-tree runner that runs one check set and caches the verdict. |
| **Workflow resource** | `sliceme.campaign`, the trusted loop that pi-subagents runs. |
| **Fingerprint** | Content hash of (tree, command vector, toolchain, policy, sandbox, checks, source) that pins a check result. |
| **Campaign approval** | One decision that covers the whole campaign commit set. `deliver` records it and consumes it when the delivery lands. |

## 3. Directory ownership

Sliceme decides ownership at plan time. There is no runtime declare step, no
lease, and no operation taxonomy.

Every DAG node declares `dir`: one repo-relative **directory**. A node must
name the deepest directory that contains each path it will add, modify, or
delete. A node is one directory slice.

```jsonc
{ "id": "api", "dir": "src/api", "tasks": ["add the parser"], "depends_on": [] }
```

Normalization:

| Input | Canonical |
|---|---|
| `src/api` | `src/api` |
| `src/api/` | `src/api` |
| `.`, ``, `/` | `.` (the repository root) |

Sliceme rejects a kind prefix (`dir:`, `file:`, `symbol:`, `api:`, `schema:`,
`config:`, `migration:`, `infra:`, `test:`) when the coordinator projects the
DAG. `dir` accepts one repository-relative directory only. It also rejects an
empty or blank `dir`. A plan that tries to own a single file, or that leaves a
blank entry, fails loudly. A node that changes no file may omit `dir`.

**One node, one directory, one visit.** The planner splits a multi-directory
node into one node per directory. The planner also merges two nodes that share a
directory into one node, so every directory has exactly one visit. The engine
rejects a node with two directories with `multi_directory_node`. The engine also
merges two nodes that share one directory, because that merge is mechanical. It
returns `revisited_directory` when the merge would make a cycle. See
[directory-slices-plan.md](./directory-slices-plan.md).

### The conflict rule

Ownership is a **subtree**. Two nodes conflict when one owned directory equals
the other, or when one contains the other. Sliceme compares directories on
path-segment boundaries. The root `.` contains every directory, so a node that
owns `.` serializes against every other node.

```
src/api       vs src/api        -> conflict (equal)
src           vs src/api        -> conflict (ancestor)
src/api       vs src/api/v1     -> conflict (descendant)
src/api       vs src/service    -> ok       (siblings)
src/models    vs src/model      -> ok       (no token similarity tier)
```

There is no fuzzy matching. A reader can explain the concurrency from the `dir`
values alone.

### Conformance: the runtime guarantee

Sliceme enforces the guarantee when the coordinator records the phase:

```text
changed = git diff --name-status HEAD   (in the campaign worktree)
owner(p) = the deepest `dir` that contains p, across all nodes
violations = [p for p in changed if owner(p) is not a current-phase node]
```

A violation raises an error and Sliceme creates no commit. The coordinator then
corrects the `dir` or adds a `depends_on` edge, and runs the node again. The
deepest-owner rule keeps a phase auditable without runtime locking.

### Authoring guidance

- Give each node the **deepest** directory that contains the work. A parent
  directory serializes its whole subtree.
- Keep same-phase directories disjoint.
- Split a node that needs two directories into two nodes, one per directory.
- Merge two nodes that would visit one directory into one node. Prefer the
  interface-first order: settle the interface, then the caller.
- Route shared build files (`BUILD`, `Cargo.toml`, lockfiles) to an explicit
  **aggregation node**. Every touched part depends on that node, and the node
  owns the shared directory. Use `.` for root files.
- Express ordering that same-directory serialization does not give you with
  `depends_on`. Never rely on a runtime queue.

## 4. The plan: `dag.json` and derived phases

`dag.json` is canonical. There is no `plan.md`. It lives under the
git-excluded `.sliceme/` directory, and Sliceme never commits it.

The plan must set `feature_branch`: the work branch (the pull request
head). `base` is the base branch (the pull request base). The coordinator
fixes the work branch before the planner runs, so the plan and the engine
agree on one branch.

```jsonc
{
  "campaign": "nanochat-cpp",
  "feature_branch": "feat/nanochat-cpp",
  "base": "master",
  "design": "DESIGN.md",
  "concurrency": 4,
  "nodes": [
    { "id": "cpu", "label": "runtime-core",
      "goal": "Tensor and the CPU reference backend for all of kernels.h",
      "dir": "backends/cpu",
      "tasks": ["add the tensor type", "add the CPU kernels"],
      "depends_on": [],
      "acceptance": ["bazel test //..."] }
  ]
}
```

| Field | Meaning |
|---|---|
| `id` | Worker id. It is a slug in `[A-Za-z0-9._-]{1,128}` and the log suffix (`worker_<id>.log`). It is never a path. |
| `label` | Human label for the report and dashboard. |
| `goal` | The prompt seed handed to the worker. |
| `dir` | The one directory the node owns, at the deepest subdirectory that contains each path (`src/api`). Directory subtrees are the only conflict unit. |
| `tasks` | The ordered work items inside the directory; the worker does them in order. |
| `depends_on` | Node ids that must reach `recorded` before this node is `ready`. |
| `acceptance` | Commands the check runner runs for this node. |
| `concurrency` | Per-phase size cap. Defaults to 3 when absent. |

**Sliceme derives the phases from the DAG.** A graph plus
the rules

```text
phase(n)  :=  max(phase(d) + 1 for d in n.depends_on), then the earliest phase with
             room (<= concurrency) and no directory-subtree conflict
ready(n) :=  n is a member of the current phase, not recorded, not running
```

are the whole scheduler. The phase is the one scheduling gate. Every dependency
lives in an earlier phase. A phase's members start together, and the engine
records and checks one phase at a time. The phase also caps how many nodes run at
once. `done` means **checked and recorded** onto
the campaign worktree. Every phase works in the same worktree, so a later phase
already sees the files of the previous phase without a merge or a rebase. To
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
 ├── sliceme.plan workflow resource
 │     └── PLANNER  (sliceme-planner, reads the design, writes dag.json)
 └── sliceme.campaign workflow resource
       ├── WORKER_*  (sliceme-worker, one per ready directory node, in the campaign worktree)
       │               edit the owned directory only -> stop  (never git)
       └── REVIEWER  (builtin reviewer, one per recorded phase)
                       reads the recorded evidence; never edits
```

The coordinator is **not** a worktree. `start` records the campaign without
creating one, so campaign setup leaves no phantom worktree in the coordinator
checkout.

### The workflow resource

The extension registers the trusted resources `sliceme.plan` and
`sliceme.campaign` in `session_start` and disposes them in `session_shutdown`.
Each resource reads the plane from the workflow `cwd`, so the caller sets `cwd`
to the repository root.

The `sliceme.plan` resource runs the planner and the bounded re-pass loop. Its
fields are:

| Field | Type | Rule |
|---|---|---|
| `campaign` | string, optional | `[A-Za-z0-9._-]{1,128}` |
| `design` | string, optional | a path without control characters; default `DESIGN.md` |

The `sliceme.campaign` resource runs the campaign loop. The `resolve` function
accepts a small set of fields. It rejects every other field.

| Field | Type | Rule |
|---|---|---|
| `campaign` | string, optional | `[A-Za-z0-9._-]{1,128}` |
| `phaseCap` | integer, optional | 1 to 64 |
| `nodeCap` | integer, optional | 1 to 256 |

The `campaign` field is the only variable text in a command. `resolve` validates
the field against the strict pattern before it builds a command.

A host grant binds an exact key and command pair. The `resolve` call cannot know
a node id or a phase index before the campaign runs. Therefore no granted command
carries a node id or a phase index. The engine reads the current phase and the
finished nodes from its own state. `resolve` uses the absolute engine path
captured in `session_start`; a relative path fails, because the workflow `cwd`
is the target repository.

| Key | Command | Use |
|---|---|---|
| `status` | `<python> <engine> --json status <campaign>` | read campaign state, phases, and `paused` |
| `ready` | `<python> <engine> --json ready <campaign>` | get the ready node ids and the current phase |
| `record` | `<python> <engine> --json record <campaign>` | commit the finished nodes |
| `check` | `<python> <engine> --json check <campaign>` | run the combined-tree checks |

The resource cannot deliver a campaign. The coordinator runs `deliver` after
the user confirms. The `deliver` action records the one campaign approval, so
there is no separate approve step. The user permission is the only gate.

### Lifecycle

```text
sliceme start <design>      # derive the work branch; planner -> dag.json + phases
sliceme open                # fetch main; create the worktree from origin/main
        │
        ▼
resource loop
  1. ready                 current-phase nodes whose dependencies reach done
  2. paused?  -> stop      return the paused state
  3. no ready node? -> stop
  4. runs.run per node     one sliceme-worker per ready node
  5. runs.all              wait for the phase children
  6. record                per-node commits on the campaign worktree
  7. check                 the combined-tree checks (evidence)
  8. runs.run reviewer     one builtin reviewer reads the recorded evidence
  9. caps?    -> stop      phaseCap or nodeCap reached
        │  repeat
        ▼
evidence + report ──► user confirms ──► deliver --confirm
                                                        (records the one approval,
                                                         pull_request: push the work branch + open a PR
                                                         local: merge the local base branch)
```

1. **`start`** derives the **work branch** from the design file name
   (`feat/<slug(design-stem)>`), or takes `--feature-branch`. Sliceme remembers
   the work branch and the **base branch** (the default branch, `main`)
   for the campaign. The work branch is never `main`, `master`, or the
   repository default. The planner then writes `dag.json`, and the engine
   projects it into phases.
2. **`ready`** returns the current phase's members that are neither recorded nor
   running, plus the phase index and `paused`. The phase is the gate, so the
   engine records and checks one phase at a time.
3. **The resource** starts one worker per ready node with the `sliceme-worker`
   agent. A worker is a one-shot pure editor in the one shared campaign
   worktree. Sliceme reuses the worktree, so it already contains every earlier
   phase's files.
4. **`record`** commits the current phase onto the campaign
   branch, with one commit per node. Sliceme attributes the commit by owned
   directories. Nothing lands on the base branch.
5. **`check`** runs the plane's trusted checks once over the recorded
   phase tree. One synchronous runner writes one terminal row, and the cache
   serves an unchanged fingerprint.
6. **The reviewer** reads the recorded evidence of step 5 and reports findings.
   The reviewer never edits. The loop checks after `record`, because a
   pi-subagents acceptance gate would test the mixed phase worktree.
7. **`evidence`** writes the deterministic evidence document (the commits, the
   checks, the diffs, and the worker logs). The coordinator runs it after the
   resource returns `complete`; the Markdown document is the pull request body.
8. **`report`** writes the deterministic skeleton plus the narrative of
   the coordinator. The coordinator runs it before the gate, so the report is
   reviewable. The coordinator shows the evidence summary and the delivery
   details: for `pull_request` the head branch, the base `main`, and the title;
   for `local` the base branch and the coming local merge. The coordinator asks
   the user to confirm one time.
9. **`deliver`** runs after every phase completes and the user confirms. The
    `confirm` flag is the confirmation token. Delivery records the one
    campaign-level approval, runs the trusted checks, and then delivers in the
    selected mode. The `pull_request` mode pushes the work branch and opens one
    pull request against the base branch (`main`). The `local` mode merges the
    work branch into the local base branch with one merge commit, and the user
    pushes later. The work branch is never the default branch.

The `sliceme.plan` resource runs the planner at most three times. A plan error
becomes the input to the next pass. The resource records nothing, so a re-pass
never rewrites a recorded node. When every pass fails, the coordinator stops and
reports the last error. A corrected `dir` or a new `depends_on` edge changes the
DAG projection, so the next `status` or `ready` reprojects the phases.

### Cleanup

Cleanup is destructive, so the agent cannot choose it without a flag. There is
no per-phase cleanup: Sliceme never removes files between phases. The `deliver`
action takes `--cleanup worktrees`. That option removes the campaign worktree,
drops a delivered work branch, and clears scratch. It keeps a landed branch
until the user pushes the base branch. It keeps the report and the `dag.json`
record. `cleanup: all` also removes every other campaign file: the
`dag.json`, `session.json`, and `control.json` records and the worker logs.

Every cleanup level keeps the report and the evidence document.

`sliceme gc` removes the files of every campaign that has finished. It keeps
the reports. A closed campaign can hold unmerged work, so `gc` keeps its files
and its branch.

## 6. Checks and sandbox

### 6.1 The single check runner

The engine has one check runner. It runs one check set over a phase tree and
returns the result. It holds no lease and queues no work.

```text
record   -> per-node commits on the campaign worktree
check           -> one check set over the combined tree
                          -> one terminal row (passed | failed | error) in `verdicts`
resume / deliver          -> the cache serves an unchanged fingerprint
```

Semantics:

- **One run.** One call runs one check set and writes one terminal row.
- **Dedupe by fingerprint.** A run whose `(tree, commands, toolchain, policy,
  sandbox, checks, source)` fingerprint already has a terminal verdict returns
  the cached row. The runner does not run the commands again.
- **The phase tree.** The checks run over the recorded phase head, so they judge
  the nodes together. Delivery runs the same runner with `source=deliver`.
- **Sandboxed.** Each run resolves the sandbox profile and wraps every command
  (see 6.2).
- **No queue.** There is no `jobs` table, no lease, and no runner process. The
  recorder holds the campaign lock, so recording and checks never interleave.

The `verdicts` table (`docs/database.md`) records the fingerprint, the phase, the
commit, the status, the results, and the timing.

### 6.2 Sandbox profiles and project manifests

`sliceme/sandbox.py` defines a `Sandbox`: a built-in mode (`none`, `bwrap`,
`unshare`) or a project `command` prefix. The profile also holds the network,
read-only, and writable policy, and the `setup` commands. Sliceme folds
`Sandbox.digest()` into the check fingerprint, so a stricter isolation or a
changed `setup` invalidates cached verdicts.

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
  "setup": ["tools/setup-deps.sh"]                // once per snapshot, before acceptance
}
```

The **planner** locates the manifest and records `"sandbox": {"path": ...}` in
`dag.json`, or `"sandbox_required": true` when the project needs isolation but
ships no manifest. The check runner resolves the gate before every run. It
refuses to continue on failure.

### 6.3 One campaign worktree for all phases

Same-phase nodes own **disjoint directory subtrees**, so they cannot author a
file collision. All phases share **one campaign
worktree** on a separate branch. The worktree is both the isolation unit and the
accumulation unit:

```text
open                          -> ONE work branch feat/<name> + worktree from origin/main
record                        -> conformance-by-ownership -> per-node commits
check                         -> the one check runner over the combined tree
(no merge per phase)           -> open N+1 in the same worktree
evidence + report             -> the deterministic evidence and report
deliver --confirm             -> records the one campaign approval, then pushes the work branch
                                 and opens a pull request against main
```

The recorder (`Service.record_phase`) stages the worktree and attributes every
changed path to exactly one same-phase node by its `dir`. It rejects an unowned,
ambiguous, or **cross-node rename** change. It then creates one commit and one
recorded record per node on the work branch. The commit subject falls back
to the DAG node `label`. A phase member that changed no file records an explicit
empty marker, so the phase still completes. The recorder diffs against the
current `HEAD`, so it never re-attributes a change from an earlier phase.

Each failure carries a reason code (`stray_path`, `ambiguous_path`,
`cross_node_rename`, or `missing_description`), so a caller switches on the code
and never parses the message.

The recorder holds the campaign lock, so it serializes with campaign creation
and other records. Sliceme reuses the worktree, so a later phase already sees
every earlier phase's files. Nothing lands on the base branch until a human
merges the pull request that the single `deliver` step opens.

Workers are pure editors. They never run `git add` or `git commit`, because the
shared index is not safe for multiple processes. They never run the suite in
the shared tree. The check runner snapshots the tree and runs the check set.

### 6.4 Several campaigns in one plane

A plane is one repository root with a `.sliceme/` directory. A plane holds the
one SQLite database, the one delivery lock, and the default branch. Several
campaigns can share the plane at the same time.

Each campaign has its own work branch, base branch, DAG file, and
delivery. The campaigns share one check runner and one database. The work
branch identifies a campaign.

Use `--campaign` to name a campaign. The value is a work branch, a branch
key, or a writer name. When a plane holds one campaign, `--campaign` is optional.
When a plane holds several campaigns, a call without `--campaign` reports the
plane summary instead of one campaign.

The engine records campaigns in the `campaigns` table. The `config.json` file
records the newest work branch for one release, so an old reader keeps
working.

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

| Role | Bound to a worktree? | Contract |
|---|---|---|
| **Coordinator** | no | owns the plan (`dag.json`), starts the resource, calls `deliver --confirm` once at the end, writes the evidence and the report |
| **Planner** | no | reads the design, writes `dag.json` |
| **Worker** | no (pure editor) | edit owned directories only, then stop; never git, never commit |
| **Reviewer** | no (read-only) | the builtin reviewer judges the recorded phase evidence, never edits |

The extension registers `sliceme-planner` and `sliceme-worker` through the
pi-subagents runtime-agent registry. Each definition carries its `tools:`
allowlist: the worker reads and edits files and runs shell commands, and the
planner reads and writes files. The worker never gets the coordinator verbs. The
resource launches the builtin `reviewer` for the phase review.

### Worker contract

1. You edit the single shared campaign worktree, launched by the resource.
2. Edit only files under the directory your DAG node owns. The phase recorder
   enforces this rule: it rejects a changed path outside the owned directories.
3. Do **not** run `git`, do **not** commit, and do **not** run the test suite.
   Stop after the edits. The engine records the phase and runs the
   combined-tree checks.
4. Never run `deliver`, `git merge`, or `git push`.

### Coordinator

Start a campaign with `/sliceme [DESIGN.md]` in pi. Run `start`, `plan`, and
`open`, then start the `sliceme.campaign` resource with `async: true`.
The resource drives `ready`, `record`, and `check`
for each phase. When it returns `complete`, run `evidence` and `report`,
show the summary and the pull request details to the user, and ask for
confirmation one time. Stop at the human gates: an explicit suspension and the
one confirmation before `deliver`.

## 8. Failure and resume

| Failure | Response |
|---|---|
| Worker produces no record / fails the checks | The coordinator retries (bounded), splits the node, or stops. |
| Reviewer reports a problem | The coordinator retries, splits the node, or stops, with the findings attached. |
| A phase record changes a path outside its node's `dir` | The record is rejected; the coordinator corrects the `dir` or adds a `depends_on` edge and runs the node again. |
| Merge conflict at `deliver` | The `merge-tree` pre-check refuses delivery; findings surface. Nothing is pushed. |
| Orchestrator crash | The pi-subagents children are children of the coordinator, so a crash kills them. On resume, a node left `running` stays `running` if the campaign worktree is present, else `pending`. Sliceme reuses the campaign worktree. Git and `state.db` win over the session descriptor. |
| User suspends (`/suspend`) | The pause flag stops new work. The adapter aborts the in-flight turn, which stops the current child within seconds. The interrupted node stays `running`, and the adapter writes `.sliceme/<branch-key>.session.json`. Resume (`/campaigns`, `pi --continue`, or `sliceme resume`) reconciles from git plus `state.db`: a node interrupted with edits in the shared worktree stays `running`, the phase is re-recorded, and unchanged records re-verify from the check cache. |
| Campaign reaches a budget cap | The adapter writes the pause flag with `reason: budget`. It never aborts the turn: the phase is the safe point, so the resource stops at its next `ready` call. The adapter marks the descriptor `suspended` with `reason: budget`. The user raises the cap with `sliceme budget`, which opens a new window and clears the flag, then resumes. |

Caps: `concurrency` (the phase size), the resource's `phaseCap` and `nodeCap`
fields, the per-command check timeout, and the optional `budget` wall-clock and
cost caps bound the cost of each campaign.
