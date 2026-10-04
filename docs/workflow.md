# Sliceme workflow

> Human documentation for the Sliceme extension. The `sliceme` tool's prompt
> guidelines repeat the workflow in short form; this file is the full
> reference. Start a campaign with `/sliceme [DESIGN.md]` or by asking the model
> to run one.

Sliceme delivers a design document as a set of parts on a **target (feature)
branch**. A top-level **coordinator** turns the design into a machine-readable
DAG (`dag.json`) and drives planner, worker, and verifier subagents. The
`sliceme` engine owns isolation, verification, and delivery. Sliceme decides
all serialization at **plan time** from directory ownership.

```text
COORDINATOR (this session)
 ├── PLANNER   reads the design, writes dag.json
 ├── WORKER_*  one-shot pure editor per ready DAG node, in the campaign worktree
 │               edit only owned dirs -> stop (the coordinator records)
 ├── VERIFIER  read-only per candidate: reviews evidence
 └── EXECUTOR  the single sandboxed runner: drains a check queue
```

- At `start` the user chooses the **target branch** once: the current branch, a
  named existing branch, or a new branch. Sliceme remembers the target for the
  whole campaign. The target is **never** `main`, `master`, or the repository
  default branch. There is no override.
- All waves commit onto one **campaign worktree** branch (for example
  `sliceme/<campaign>`), forked once from the target. Sliceme never recreates or
  rebases the worktree. Files from an earlier wave are still present for the
  next wave.
- The DAG is the **only authored schedule**. Sliceme computes waves as a
  deterministic projection of it: it packs nodes into waves by `owns`
  **directory-subtree overlap** and `depends_on`, capped by `concurrency`
  (default 3). `ready(n) := every d in n.depends_on is done` **and** `n` is in
  the current wave. `done` means verified **and recorded** onto the campaign
  worktree.
- The planner **merges same-ownership chains**. Sliceme normalizes the DAG
  automatically before every wave projection: when two nodes own the same
  directory set and one depends on the other, it contracts them into one node.
  One subagent then completes the whole directory change and the engine records
  one commit. A node sets `no_merge` to stay separate.
- A node owns **directories, not files**. For each path it will add, modify, or
  delete, it declares the deepest directory that contains it (`dir:src/api`).
  Sliceme serializes two nodes with overlapping owned directories (equal,
  ancestor, or descendant) into different waves. There is no runtime declare
  step and no lease.
- A node starts only in the current wave. A later wave begins after the
  coordinator records and verifies every member of the previous wave. The node
  runs in the same worktree, so it already sees the files of the previous wave.
- Workers are **pure editors**: they edit only their owned directories, never
  run `git`, and never run the test suite. The coordinator runs
  `wave --record --wave N` to create one commit per node and enforce
  conformance-by-ownership. The commit subject is the node's human description;
  the wave stays in the DAG and the state, not in the subject.
- Only the executor runs checks and only the executor may use the GPU. Verifiers
  delegate to the executor and judge its recorded evidence.
- The executor is a **single serialized runner** (`exec --run`/`--wait`) over a
  SQLite-backed queue. It runs each job in a **sandbox** resolved by
  `sliceme/sandbox.py`; the sandbox digest is part of the verification
  fingerprint, so tightening isolation invalidates cached verdicts.
- The **target repository** provides *how to run tests in isolation* as a
  tracked `sliceme.sandbox.json` (never under `.sliceme/`). The planner records
  its path in `dag.json`. The coordinator runs `exec --validate` before it
  verifies, and refuses to continue when a required sandbox does not exist.
- `tools/gpu.sh` is **sliceme's** broker, shipped with the package and invoked
  by resolved path. A project can override the GPU invocation through its
  sandbox manifest. The target repository owns *how to run tests in isolation*,
  not sliceme's GPU locking policy.
- Nothing merges per wave. Commits accumulate on the campaign worktree. A human
  approves individual commits, or all of them, in the local review client
  (`docs/review.md`), before or after the last wave. When every wave completes
  and a human approves every commit, the coordinator runs `deliver`
  automatically and merges with the trusted checks.
- `wave --record` enforces **plan conformance**: it rejects a changed path
  outside the owned directories of its node. The coordinator then widens `owns`
  or adds a `depends_on` edge (the DAG fingerprint changes, so the next
  `status`/`ready`/`spawn` replans).
- Sliceme can reconstruct everything from `.sliceme/` plus git after a crash.

The pi package ships two extension modules. They register two tools, `sliceme`
for the coordinator and `sliceme-unit` for workers, plus the
`/sliceme [DESIGN.md]` command. Both tools register **inactive**; `/sliceme`
activates them for the session. There is no separate skill: the workflow lives
here and in the prompt guidelines of the tools. `runSubagent` applies each
subagent's `tools:` allowlist, so a worker gets `sliceme-unit` but never
`sliceme`, and the verifier gets neither.

## Hard rules

1. **Workers run inside the one campaign worktree.** They edit only their
   owned directories and never touch the main working tree.
2. **Edit only your node's owned directories.** The planner declares ownership
   in `dag.json`, and the wave recorder (`wave --record --wave N`) enforces it.
   A rejection means the planner under-declared; do not widen your own scope.
3. **Finish without landing.** Workers are pure editors: edit only your owned
   directories and stop. Never run `git`, never commit, never call
   `sliceme-unit` `deliver`, and never call the `sliceme` coordinator tool. The
   coordinator owns recording and delivery.
4. **The planner authors ordering in the DAG.** If two nodes would touch the
   same directory, the planner must put them in different waves (disjoint
   `owns`) or add a `depends_on` edge. Never rely on runtime arbitration.
5. **The GPU is the executor's.** The single executor runs checks and composes
   the sandbox and GPU runner. Workers never touch the GPU, and verifiers only
   judge the recorded evidence of the executor.
6. **Never commit to the default branch.** The target is always a feature branch
   chosen at start. `deliver` refuses `main`, `master`, and the repository
   default branch with no override.

## Campaign loop (the coordinator)

Use the `sliceme` tool:

```
sliceme start <DESIGN.md>   choose target branch + planner -> dag.json + waves
sliceme ready               current-wave nodes whose dependencies are done
sliceme status              waves + DAG + live child state
sliceme spawn <node>        one-shot pure editor in the campaign worktree
sliceme record              commit the current wave onto the campaign worktree
sliceme verify <node>       executor runs checks; a read-only verifier judges
sliceme review --serve      local review client (per-commit approval)
sliceme deliver             merge to target once every commit is approved
sliceme review --report     deterministic report (`--narrative` appends the summary)
```

The coordinator also registers two commands and two lifecycle hooks
(`docs/sessions.md`):

```
/suspend [label]            park the campaign and write the resume descriptor
/campaigns                  list registered campaigns and switch to one
```

`start` asks for the target branch, projects the DAG into waves
(directory-subtree overlap, `depends_on` barrier, `concurrency` cap; default 3),
and stores them in `state.json`. A node may spawn only in the current wave. When
the coordinator records and verifies every member of a wave, the next wave opens
in the same worktree. A coordinator-added `depends_on` edge changes the DAG
fingerprint, and the next `status`/`ready`/`spawn` automatically replans the
waves.

The coordinator's own checkout is **not** an Sliceme unit. `sliceme start`
bootstraps the plane with `--no-unit` and records the chosen target branch. The
campaign work commits to a separate worktree branch. `deliver` merges it into
the target only after every wave completes and a human approves every
accumulated commit in the review client. Sliceme refuses `main`, `master`, and
the default branch at both steps.

## Engine actions (both tools)

| Action | Purpose |
|---|---|
| `start` | bootstrap the plane; `no_unit: true` for the coordinator's checkout; `target`/`target_mode` chooses the feature branch; `name` + `base` creates a worker unit |
| `status` | units, candidates, waves; `short`, `unit`, `simulate`, `health`, `gc`; `--sessions` lists registered campaigns; `--resume` reconciles a suspended campaign from git plus `state.db` |
| `deliver` | merge the campaign worktree into the target feature branch; `target`, `source`, `cleanup`, `no_checks` |
| `exec` | the sandboxed executor: `--validate` (sandbox gate), `--submit`/`--run`/`--wait`/`--cancel` check jobs |
| `wave` | the campaign worktree: `--open` (create/reuse it), `--record --wave N` (per-node commits, conformance) |
| `review` | local review: `--serve`, `--state`, `--diff`, `--poll`, `--ack`, `--comment`, `--decision` (`--commit` or `--all`); `--report --narrative` writes the deterministic report |
| `attempt` | persist a subagent attempt's `--begin`/`--end` and metrics |

The coordinator adds the `ready`, `spawn`, `record`, `verify`, and `report`
verbs on top.

## Worker workflow

A spawned worker edits the shared campaign worktree and does nothing else:

```
# ... edit only files under your node's owned directories ...
# no git, no commit, no test run; stop and report
```

Workers are pure editors: they edit only their owned directories and stop. The
coordinator runs `wave --record --wave N` to enforce conformance, create
per-node commits, and run the checks through the single executor. A worker never
runs the suite, never runs `git`, and never touches the GPU. Nothing merges to
the target branch until a human approves every commit and the coordinator runs
its single `deliver` step.

Ownership syntax is `dir:PATH` (a bare path is also accepted). It is always a
directory at the deepest level that contains the paths the node touches:
`dir:src/api`, `dir:src`, or `dir:.` for repository-root files. Sliceme rejects
a non-directory spec (`file:`, `symbol:`, ...) when the coordinator projects the
DAG, so a campaign cannot start with file-level ownership.

If the wave recorder rejects a path outside the owned directories, report it and
stop. The coordinator widens the node's `owns` (or adds a `depends_on` edge) in
`dag.json`. The next `status`/`ready`/`spawn` replans the waves.

Full specification: [guide.md](./guide.md). Tool reference:
[reference.md](./reference.md).
