# Sliceme workflow

> Human documentation for the Sliceme extension. The `sliceme` tool's prompt
> guidelines repeat the workflow in short form; this file is the full
> reference. Start a campaign with `/sliceme [DESIGN.md]` or by asking the model
> to run one.
>
> **Status.** This document describes the directory-slice design: one node per
> directory. The engine change is implemented, and the GPU field is removed. See
> [directory-slices-plan.md](./directory-slices-plan.md).

Sliceme delivers a design document as a set of parts on a **work branch**
(`feat/<name>`). A top-level **coordinator** turns the design into a
machine-readable DAG (`dag.json`). The trusted `sliceme.campaign` workflow
resource then runs the loop. The `sliceme` engine owns isolation, checks, and
delivery. Sliceme decides all serialization at **plan time** from directory
ownership.

```text
COORDINATOR (this session)
 ├── PLANNER   reads the design, writes dag.json
 └── sliceme.campaign workflow resource
       ├── WORKER_*  one-shot pure editor per ready DAG node, in the campaign worktree
       │               edit only owned dirs -> stop (the resource records)
       └── REVIEWER  builtin reviewer per recorded phase: reads the evidence
```

- At `start` the engine derives the **work branch** from the design file
  name (`feat/<slug(design-stem)>`), or the user passes `--feature-branch`.
  Sliceme records the work branch and the **base branch** (the default
  branch, `main`) for the whole campaign. The work branch is the pull
  request head and is **never** `main`, `master`, or the repository default
  branch. There is no override.
- All phases commit onto the **work branch**. `open` fetches the
  base branch and creates the worktree from `origin/<base_branch>`, so the
  worktree holds the newest default-branch commits. Sliceme never recreates or
  rebases the worktree. Files from an earlier phase are still present for the
  next phase.
- The DAG is the **only authored schedule**. Sliceme computes phases as a
  deterministic projection of it: it packs nodes into phases by `dir`
  **directory-subtree overlap** and `depends_on`, capped by `concurrency`
  (default 3). `ready(n)` returns the current phase's members that are neither
  recorded nor running. The phase is the one scheduling gate; `done` means
  checked **and** recorded onto the campaign worktree.
- **One node is one directory slice.** Every node declares one `dir`, and every
  directory appears in exactly one node. The planner splits a multi-directory
  node into one node per directory. The planner also merges two nodes that
  would visit one directory into one node. The engine rejects a plan that breaks
  either rule. See [directory-slices-plan.md](./directory-slices-plan.md).
- The planner merges a revisited directory in a second pass, so the DAG never
  contains two nodes for one directory. Prefer the interface-first order:
  settle the interface, then the caller.
- A node owns **a directory, not files**. For each path it will add, modify, or
  delete, it declares the deepest directory that contains it (`src/api`).
  Sliceme serializes two nodes with overlapping directories (equal, ancestor, or
  descendant) into different phases. It rejects an empty or blank `dir`. There is
  no runtime declare step and no lease.
- A node starts once the engine reaches its phase and every dependency reaches
  `done`. The phase is the scheduling gate: one phase records and checks before
  the next phase starts. The node runs in the same worktree, so it already sees
  the files of the previous phase.
- Workers are **pure editors**: they edit only their owned directory, never
  run `git`, and never run the test suite. The resource runs
  `record` to create one commit per node and enforce
  conformance-by-ownership. The commit subject is the node's human description;
  the phase stays in the DAG and the state, not in the subject.
- One **check runner** runs the checks. It runs one check set over the recorded
  phase tree and writes one terminal row to the `verdicts` table. The cache serves
  an unchanged fingerprint, so a resumed node does not re-run a full check.
- The one check runner resolves the **sandbox** through `sliceme/sandbox.py`;
  the sandbox digest is part of the check fingerprint, so tightening isolation
  invalidates cached verdicts.
- The **target repository** provides *how to run tests in isolation* as a
  tracked `sliceme.sandbox.json` (never under `.sliceme/`). The planner records
  its path in `dag.json`. The check runner refuses to continue when a required
  sandbox does not exist.
- Nothing lands on the base branch per phase. Commits accumulate on the
  work branch. The engine writes the evidence document after the last phase.
  The coordinator shows the evidence summary and the pull request details to
  the user and asks for confirmation one time. On a confirmation the
  coordinator runs `deliver --confirm`. Delivery records the one campaign
  approval and opens one pull request against the base branch (`main`) with
  the trusted checks.
- `record` enforces **plan conformance**: it rejects a changed path
  outside the `dir` of its node, with a reason code. The
  coordinator then corrects the `dir` or adds a `depends_on` edge and runs the
  node again.
- Sliceme can reconstruct everything from `.sliceme/` plus git after a crash.

The pi package ships one extension module. It registers the `sliceme` engine
tool, the `sliceme.plan` and `sliceme.campaign` workflow resources, and the
`sliceme-planner` and `sliceme-worker` agent definitions. The tool registers **inactive**; `/sliceme`
activates it for the session. There is no separate skill: the workflow lives
here and in the prompt guidelines of the tool. The resource launches each
`sliceme-worker` with its `tools:` allowlist, so a worker never gets the
coordinator verbs. The resource launches the builtin `reviewer` for the phase
review.

## Hard rules

1. **Workers run inside the one campaign worktree.** They edit only their
   owned directories and never touch the main working tree.
2. **Edit only your node's owned directories.** The planner declares ownership
   in `dag.json`, and the phase recorder (`record`) enforces it. A
   rejection means the planner under-declared; do not widen your own scope.
3. **Finish without landing.** Workers are pure editors: edit only your owned
   directories and stop. Never run `git`, never commit, never run `git push`,
   and never call the engine. The resource owns the phase record. The
   coordinator owns delivery.
4. **The planner authors ordering in the DAG.** If two nodes would touch the
   same directory, the planner must give them one node or add a `depends_on`
   edge. Never rely on runtime arbitration.
5. **The reviewer only reads.** The one check runner runs the checks. Workers
   never run the suite, and the reviewer judges the recorded evidence.
6. **Never push the default branch.** The work branch is the delivery head and
   is never `main`, `master`, or the repository default branch. `deliver`
   refuses a work branch that equals the default branch, with no override. The
   delivery base is the base branch (`main`). A `local` delivery merges into
   the local base branch only; the user pushes it later.

## Planning and campaign loops (the coordinator)

Use the `sliceme` tool, then start the resources:

```text
sliceme start DESIGN.md     derive the work branch
subagent(workflow: "sliceme.plan", cwd: <repo>, async: false, args: {design: "DESIGN.md"})
sliceme plan --design DESIGN.md   the design's campaign split and the next entry
sliceme open         fetch the base branch; create the worktree from origin/main
subagent(workflow: "sliceme.campaign", cwd: <repo>, async: true)
```

The `sliceme.plan` resource runs the bounded planning loop itself:

```text
runs.run sliceme-planner    write dag.json (pass 1)
ready                       validate the DAG and plan the phases
...                         on a plan error, run the planner again with the
                            error text, up to three passes, then "failed"
```

The planning loop stops after three passes. A plan error becomes the input to
the next planner pass. The resource records no node, so a re-pass never rewrites
a recorded node. On `failed` the coordinator stops and reports the last error.

The `sliceme.campaign` resource runs the campaign loop itself:

```text
ready                       the current phase's ready node ids, plus paused
record                      commit the finished nodes as per-node commits
check                       run the combined-tree checks for the phase
runs.run reviewer           one builtin reviewer reads the recorded evidence
```

When the resource returns `complete`, the coordinator writes the evidence and
shows the summary to the user, then runs:

```text
sliceme evidence                    write the deterministic evidence document
sliceme report                      the deterministic report (--narrative appends the summary)
sliceme deliver --confirm [--mode MODE]  pull_request: push + open a PR against main
                                    local: merge the local base branch
```

The resource bounds the loop with the `phaseCap` and `nodeCap` fields. An
optional wall-clock or cost cap stops the campaign at a phase boundary
(`docs/sessions.md` §9); the resource returns `state: "paused"` with the engine's
pause reason. The coordinator also registers two commands and two lifecycle
hooks (`docs/sessions.md`):

```text
/suspend [label]            park the campaign and write the resume descriptor
/campaigns                  list registered campaigns and switch to one
```

The coordinator's own checkout is **not** a Sliceme worktree. `sliceme start`
bootstraps the plane and records the work branch and the base branch. The
campaign work commits to the work branch. `deliver` pushes that branch and
opens a pull request against the base branch (`main`). This step waits for
every phase and for the user confirmation. Sliceme refuses a work branch that
equals the default branch.

## Engine actions

| Action | Purpose |
|---|---|
| `start` | bootstrap the plane; `design` derives the work branch; `feature_branch` overrides it; `base` overrides the base branch; `budget_seconds` and `budget_usd` set the caps |
| `status` | the campaign state: records, phases, and checks; `short`, `unit`, `dense`, `verbose` |
| `sessions` | list the registered campaigns from their session descriptors |
| `resume` | reconcile a suspended campaign from git plus `state.db` and return its resume plan |
| `budget` | set, raise, or clear the campaign wall-clock and cost caps |
| `gc` | prune worktrees, delivered branches, scratch, and finished campaign files |
| `doctor` | check git, `config.json`, and `state.db` health |
| `ready` | the current-phase ready node ids, the phase index, and `paused` |
| `plan` | the design's `sliceme-campaigns` split joined with the registry state, plus the next entry |
| `deliver` | confirm and deliver the campaign; `confirm` records the one campaign approval; `mode` selects `pull_request` (push + PR) or `local` (merge the local base branch); `actor` and `note` join the approval row; `source`, `cleanup`, `no_checks` |
| `check` | run the one combined-tree check runner for the current phase |
| `open` | create or reuse the campaign worktree |
| `record` | record the current phase: conformance + per-node commits |
| `report` | write the deterministic report (`--narrative` appends the summary) |
| `evidence` | write the deterministic evidence document: the commits, the checks, the diffs, and the worker logs |

## Worker workflow

The resource launches a worker for each ready node. The worker edits the shared
campaign worktree and does nothing else:

```text
# ... edit only files under your node's owned directories ...
# no git, no commit, no test run; stop and report
```

Workers are pure editors: they edit only their owned directories and stop. The
resource runs `record` to enforce conformance and create
per-node commits, then `check` runs the combined-tree checks. A worker
never runs the suite, never runs `git`, and never runs the tests. Nothing lands
on the base branch until the human gate. The coordinator's single `deliver`
step then opens a pull request, or merges the local base branch.

Ownership syntax is a repository-relative directory path. It is always a
directory at the deepest level that contains the paths the node touches:
`src/api`, `src`, or `.` for repository-root files. Sliceme rejects a kind
prefix (`dir:`, `file:`, `symbol:`, ...) when the coordinator projects the DAG,
so a campaign cannot start with file-level ownership.

If the phase recorder rejects a path outside the owned directories, report it and
stop. The coordinator corrects the node's `dir` (or adds a `depends_on` edge) in
`dag.json`. The next `status` or `ready` reprojects the phases.

Full specification: [guide.md](./guide.md). Tool reference:
[reference.md](./reference.md).
