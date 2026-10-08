# Sliceme workflow

> Human documentation for the Sliceme extension. The `sliceme` tool's prompt
> guidelines repeat the workflow in short form; this file is the full
> reference. Start a campaign with `/sliceme [DESIGN.md]` or by asking the model
> to run one.

Sliceme delivers a design document as a set of parts on a **campaign branch**
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
       └── REVIEWER  builtin reviewer per recorded wave: reads the evidence
```

- At `start` the engine derives the **campaign branch** from the design file
  name (`feat/<slug(design-stem)>`), or the user passes `--feature-branch`.
  Sliceme records the campaign branch and the **delivery base** (the default
  branch, `main`) for the whole campaign. The campaign branch is the pull
  request head and is **never** `main`, `master`, or the repository default
  branch. There is no override.
- All waves commit onto the **campaign branch**. `wave --open` fetches the
  delivery base and creates the worktree from `origin/<delivery_base>`, so the
  worktree holds the newest default-branch commits. Sliceme never recreates or
  rebases the worktree. Files from an earlier wave are still present for the
  next wave.
- The DAG is the **only authored schedule**. Sliceme computes waves as a
  deterministic projection of it: it packs nodes into waves by `owns`
  **directory-subtree overlap** and `depends_on`, capped by `concurrency`
  (default 3). `ready(n) := every d in n.depends_on reaches done`. Readiness is
  the spawn gate; the wave stays a display hint. `done` means checked **and**
  recorded onto the campaign worktree.
- A node with a `gpu` tier also conflicts with every other node. The projection
  places a GPU node in a wave of its own, so exactly one GPU node runs at a
  time. There is no serialized broker.
- The planner **merges same-ownership chains**. Sliceme normalizes the DAG
  automatically before every wave projection: when two nodes own the same
  directory set and one depends on the other, it contracts them into one node.
  One subagent then completes the whole directory change and the engine records
  one commit. A node sets `no_merge` to stay separate.
- A node owns **directories, not files**. For each path it will add, modify, or
  delete, it declares the deepest directory that contains it (`dir:src/api`).
  Sliceme serializes two nodes with overlapping owned directories (equal,
  ancestor, or descendant) into different waves. It rejects an empty or blank
  `owns` entry, and a node that changes no file may omit `owns`. There is no
  runtime declare step and no lease.
- A node starts once every dependency reaches `done`; the wave stays a display
  hint. The node runs in the same worktree, so it already sees the files of the
  previous wave.
- Workers are **pure editors**: they edit only their owned directories, never
  run `git`, and never run the test suite. The resource runs
  `wave --record --current` to create one commit per node and enforce
  conformance-by-ownership. The commit subject is the node's human description;
  the wave stays in the DAG and the state, not in the subject.
- One **check runner** runs the checks. It runs one check set over the recorded
  wave tree and writes one terminal row to the `checks` table. The cache serves
  an unchanged fingerprint, so a resumed node does not re-run a full check.
- The one check runner resolves the **sandbox** through `sliceme/sandbox.py`;
  the sandbox digest is part of the check fingerprint, so tightening isolation
  invalidates cached verdicts.
- The **target repository** provides *how to run tests in isolation* as a
  tracked `sliceme.sandbox.json` (never under `.sliceme/`). The planner records
  its path in `dag.json`. The check runner refuses to continue when a required
  sandbox does not exist.
- `tools/gpu.sh` is **sliceme's** host runner, shipped with the package and
  invoked by resolved path. The check runner composes it outside the project
  sandbox for a GPU job. A project can override the GPU invocation through its
  sandbox manifest. The scheduler owns the one-GPU-lane rule.
- Nothing lands on the delivery base per wave. Commits accumulate on the
  campaign branch. The engine writes the evidence document after the last wave.
  The coordinator shows the evidence summary and the pull request details to
  the user and asks for confirmation one time. On a confirmation the
  coordinator records the approval and runs `deliver`, which opens one pull
  request against the delivery base (`main`) with the trusted checks.
- `wave --record` enforces **plan conformance**: it rejects a changed path
  outside the owned directories of its node. `wave --record --only <node>`
  scopes the record to one node and fails that node alone with a reason code.
  The coordinator then widens `owns` or adds a `depends_on` edge and respawns.
- Sliceme can reconstruct everything from `.sliceme/` plus git after a crash.

The pi package ships one extension module. It registers the `sliceme` engine
tool, the `sliceme.campaign` workflow resource, and the `sliceme-planner` and
`sliceme-worker` agent definitions. The tool registers **inactive**; `/sliceme`
activates it for the session. There is no separate skill: the workflow lives
here and in the prompt guidelines of the tool. The resource launches each
`sliceme-worker` with its `tools:` allowlist, so a worker never gets the
coordinator verbs. The resource launches the builtin `reviewer` for the wave
review.

## Hard rules

1. **Workers run inside the one campaign worktree.** They edit only their
   owned directories and never touch the main working tree.
2. **Edit only your node's owned directories.** The planner declares ownership
   in `dag.json`, and the wave recorder (`wave --record`) enforces it. A
   rejection means the planner under-declared; do not widen your own scope.
3. **Finish without landing.** Workers are pure editors: edit only your owned
   directories and stop. Never run `git`, never commit, never run `git push`,
   and never call the engine. The resource owns the wave record. The
   coordinator owns delivery.
4. **The planner authors ordering in the DAG.** If two nodes would touch the
   same directory, the planner must put them in different waves (disjoint
   `owns`) or add a `depends_on` edge. Never rely on runtime arbitration.
5. **The GPU is the check runner's.** The one check runner composes the sandbox
   and the GPU runner. Workers never touch the GPU, and the reviewer only judges
   the recorded evidence.
6. **Never push the default branch.** The campaign branch is the pull request
   head and is never `main`, `master`, or the repository default branch.
   `deliver` refuses a campaign branch that equals the default branch, with no
   override. The pull request base is the delivery base (`main`).

## Campaign loop (the coordinator)

Use the `sliceme` tool, then start the resource:

```text
sliceme start DESIGN.md     derive the campaign branch; planner -> dag.json + waves
sliceme plan --design DESIGN.md   the design's campaign split and the next entry
sliceme wave --open         fetch the delivery base; create the worktree from origin/main
subagent(workflow: "sliceme.campaign", cwd: <repo>, async: true)
```

The resource runs the loop itself:

```text
ready                       current-wave nodes whose dependencies reach done, plus paused
wave --record --current     commit the finished nodes as per-node commits
check --current             run the combined-tree checks for the wave
runs.run reviewer           one builtin reviewer reads the recorded evidence
```

When the resource returns `complete`, the coordinator writes the evidence and
shows the summary to the user, then runs:

```text
sliceme evidence                    write the deterministic evidence document
sliceme review --report             the deterministic report (--narrative appends the summary)
sliceme review --decision approve   record the one campaign approval (after the user confirms)
sliceme deliver                     push the campaign branch and open a pull request against main
```

The resource bounds the loop with the `waveCap` and `nodeCap` fields. The
coordinator also registers two commands and two lifecycle hooks
(`docs/sessions.md`):

```text
/suspend [label]            park the campaign and write the resume descriptor
/campaigns                  list registered campaigns and switch to one
```

The coordinator's own checkout is **not** an Sliceme unit. `sliceme start`
bootstraps the plane with `--no-unit` and records the campaign branch and the
delivery base. The campaign work commits to the campaign branch. `deliver`
pushes that branch and opens a pull request against the delivery base (`main`).
This step waits for every wave and for the user confirmation. Sliceme refuses a
campaign branch that equals the default branch.

## Engine actions

| Action | Purpose |
|---|---|
| `start` | bootstrap the plane; `design` derives the campaign branch; `feature_branch` overrides it; `base` overrides the delivery base; `no_unit` for the coordinator's checkout; `name` creates a worker unit |
| `status` | units, candidates, waves, checks; `short`, `unit`, `simulate`, `health`, `gc`; `--sessions` lists registered campaigns; `--resume` reconciles a suspended campaign from git plus `state.db` |
| `ready` | the current-wave ready node ids, the wave index, and `paused` |
| `plan` | the design's `sliceme-campaigns` split joined with the registry state, plus the next entry |
| `deliver` | push the campaign branch and open the delivery pull request against the delivery base (`main`); `source`, `cleanup`, `no_checks` |
| `check` | run the one combined-tree check runner for the current wave (`--current`) |
| `wave` | the campaign worktree: `--open` (create/reuse it), `--record --current` (per-node commits, conformance) |
| `review` | `--decision approve\|request_changes\|override` (one campaign decision); `--report --narrative` writes the deterministic report |
| `evidence` | write the deterministic evidence document: the commits, the checks, the diffs, and the worker logs |

## Worker workflow

The resource launches a worker for each ready node. The worker edits the shared
campaign worktree and does nothing else:

```text
# ... edit only files under your node's owned directories ...
# no git, no commit, no test run; stop and report
```

Workers are pure editors: they edit only their owned directories and stop. The
resource runs `wave --record --current` to enforce conformance and create
per-node commits, then `check --current` runs the combined-tree checks. A worker
never runs the suite, never runs `git`, and never touches the GPU. Nothing lands
on the delivery base until a human merges the pull request that the
coordinator's single `deliver` step opens.

Ownership syntax is `dir:PATH` (a bare path is also accepted). It is always a
directory at the deepest level that contains the paths the node touches:
`dir:src/api`, `dir:src`, or `dir:.` for repository-root files. Sliceme rejects
a non-directory spec (`file:`, `symbol:`, ...) when the coordinator projects the
DAG, so a campaign cannot start with file-level ownership.

If the wave recorder rejects a path outside the owned directories, report it and
stop. The coordinator widens the node's `owns` (or adds a `depends_on` edge) in
`dag.json`. The next `status` or `ready` reprojects the waves.

Full specification: [guide.md](./guide.md). Tool reference:
[reference.md](./reference.md).
