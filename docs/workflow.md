# Sliceme workflow

> Human documentation for the Sliceme extension. The workflow below is also
> condensed into the `sliceme` tool's prompt guidelines; this file is the full
> reference. Start a campaign with `/sliceme [DESIGN.md]` or by asking the model
> to run one.

Sliceme delivers a design document as a set of components on a **target
(feature) branch**. A top-level **coordinator** turns the design into a
machine-readable DAG (`dag.json`) and drives planner, worker, and verifier
subagents. The `sliceme` engine owns isolation, verification, and delivery; all
serialization is decided at **plan time** from directory ownership.

```text
COORDINATOR (this session)
 ├── PLANNER   reads the design, writes dag.json
 ├── WORKER_*  one-shot pure editor per ready DAG node, in the campaign worktree
 │               edit only owned dirs -> stop (the coordinator records)
 ├── VERIFIER  read-only per candidate: reviews evidence
 └── EXECUTOR  the single sandboxed runner: drains a check queue
```

- At `start` the user chooses the **target branch** once: the current branch, a
  named existing branch, or a new branch. The target is remembered for the
  whole campaign and is **never** `main`, `master`, or the repository default
  branch. There is no override.
- All waves commit onto one **campaign worktree** branch (for example
  `sliceme/<campaign>`), forked once from the target. The worktree is never
  recreated and never rebased; files from an earlier wave are still present for
  the next wave.
- The DAG is the **only authored schedule**. Waves are a deterministic
  projection of it: nodes are packed into waves by `owns` **directory-subtree
  overlap** and `depends_on`, capped by `concurrency` (default 3).
  `ready(n) := every d in n.depends_on is done` **and** `n` is in the current
  wave, where `done` means verified **and recorded** onto the campaign
  worktree.
- A node owns **directories, not files**. For each path it will add, modify, or
  delete it declares the deepest directory that contains it (`dir:src/api`).
  Two nodes whose owned directories overlap (equal, ancestor, or descendant)
  are serialized into different waves. There is no runtime declare/lease step.
- A node starts only in the current wave; a later wave begins after every
  member of the previous wave is recorded and verified. It runs in the same
  worktree, so it already sees the previous wave's files.
- Workers are **pure editors**: they edit only their owned directories, never
  run `git`, and never run the test suite. The coordinator runs
  `wave --record --wave N` to create one commit per node and enforce
  conformance-by-ownership.
- Only the executor runs checks and only the executor may use the GPU. Verifiers
  delegate to the executor and judge its recorded evidence.
- The executor is a **single serialized runner** (`exec --run`/`--wait`) over a
  SQLite-backed queue. It runs each job in a **sandbox** resolved by
  `sliceme/sandbox.py`; the sandbox digest is part of the verification
  fingerprint, so tightening isolation invalidates cached verdicts.
- The **target repository** provides *how to run tests in isolation* as a
  tracked `sliceme.sandbox.json` (never under `.sliceme/`). The planner records
  its path in `dag.json`; the coordinator runs `exec --validate` before
  verifying and refuses to continue when a required sandbox is missing.
- `tools/gpu.sh` is **sliceme's** broker, shipped with the package and invoked
  by resolved path; a project may override the GPU invocation through its
  sandbox manifest. The target repository owns *how to run tests in isolation*,
  not sliceme's GPU locking policy.
- Nothing is merged per wave. Commits accumulate on the campaign worktree. A
  human approves individual commits, or all of them, in the local review client
  (`docs/review.md`), before or after the last wave. When every wave is done and
  every commit is approved, the coordinator runs `deliver` automatically and
  merges with the trusted checks.
- `wave --record` enforces **plan conformance**: a changed path outside its
  node's owned directories is rejected, and the coordinator widens `owns` or
  adds a `depends_on` edge (the DAG fingerprint changes, so the next
  `status`/`ready`/`spawn` replans).
- Everything is reconstructable from `.sliceme/` + git after a crash.

The pi package is a single extension that registers two tools: `sliceme` for the
coordinator and `sliceme-unit` for workers, plus the `/sliceme [DESIGN.md]`
command. Both tools register **inactive**; `/sliceme` activates them for the
session. There is no separate skill: the workflow lives here and in the tools'
prompt guidelines. `runSubagent` applies each subagent's `tools:` allowlist, so
a worker gets `sliceme-unit` but never `sliceme`, and the verifier gets neither.

## Hard rules

1. **Workers run inside the one campaign worktree.** They edit only their
   owned directories and never touch the main working tree.
2. **Edit only your node's owned directories.** Ownership is declared in
   `dag.json` and enforced by the wave recorder (`wave --record --wave N`); a
   rejection means the planner under-declared, not that you should widen your
   own scope.
3. **Finish without landing.** Workers are pure editors: edit only your owned
   directories and stop. Never run `git`, never commit, never call
   `sliceme-unit` `deliver`, and never call the `sliceme` coordinator tool; the
   coordinator owns recording and delivery.
4. **Ordering is authored in the DAG.** If two nodes would touch the same
   directory, the planner must put them in different waves (disjoint `owns`) or
   add a `depends_on` edge. Never rely on runtime arbitration.
5. **The GPU is the executor's.** The single executor runs checks and composes
   the sandbox and GPU runner; workers never touch the GPU, and verifiers only
   judge the executor's recorded evidence.
6. **Never commit to the default branch.** The target is always a feature branch
   chosen at start; `deliver` refuses `main`, `master`, and the repository
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
(directory-subtree overlap, `depends_on` barrier, `concurrency` cap; default 3)
and stores them in `state.json`. A node may only spawn in the current wave;
when every member of a wave is recorded and verified the next wave opens in the
same worktree. A coordinator-added `depends_on` edge changes the DAG fingerprint
and the next `status`/`ready`/`spawn` automatically replans the waves.

The coordinator's own checkout is **not** an Sliceme unit; `sliceme start`
bootstraps the plane with `--no-unit` and records the chosen target branch. The
campaign work commits to a separate worktree branch, and `deliver` merges it
into the target only after every wave is done and every accumulated commit is
approved in the review client. `main`, `master`, and the default branch are
refused at both steps.

## Unit actions (the `sliceme-unit` tool)

| Action | Purpose |
|---|---|
| `start` | bootstrap the plane; `no_unit: true` for the coordinator's checkout; `target`/`target_mode` chooses the feature branch; `name` + `base` creates a worker unit |
| `status` | units, candidates, waves; `short`, `unit`, `simulate`, `health`, `gc` |
| `deliver` | merge the campaign worktree into the target feature branch; `target`, `source`, `cleanup`, `no_checks` |
| `status` | `--sessions` lists registered campaigns; `--resume` reconciles a suspended campaign from git plus `state.db` |
| `review` | local review and the deterministic report (`--report --narrative`) |
| `attempt` | persist a subagent attempt's `--begin`/`--end` and metrics |
| `review` | local review: `--serve`, `--state`, `--diff`, `--poll`, `--ack`, `--comment`, `--decision` (`--commit` or `--all`) |
| `wave` | the campaign worktree: `--open` (create/reuse it), `--record --wave N` (per-node commits, conformance) |
| `exec` | the sandboxed executor: `--validate` (sandbox gate), `--submit`/`--run`/`--wait`/`--cancel` check jobs |

## Worker workflow

A spawned worker edits the shared campaign worktree and does nothing else:

```
# ... edit only files under your node's owned directories ...
# no git, no commit, no test run; stop and report
```

Workers are pure editors: they edit only their owned directories and stop — the
coordinator runs `wave --record --wave N` to enforce conformance, create
per-node commits, and run the checks through the single executor. A worker never
runs the suite, never runs `git`, and never touches the GPU. Nothing is merged
to the target branch until every commit is approved and the coordinator runs
its single `deliver` step.

Ownership syntax is `dir:PATH` (a bare path is also accepted), always a
directory at the deepest level that contains the paths the node touches:
`dir:src/api`, `dir:src`, or `dir:.` for repository-root files. A non-directory
spec (`file:`, `symbol:`, ...) is rejected when the DAG is projected, so a
campaign cannot start with file-level ownership.

If the wave recorder rejects a path outside the owned directories, report it and stop.
The coordinator widens the node's `owns` (or adds a `depends_on` edge) in
`dag.json`; the next `status`/`ready`/`spawn` replans the waves.

Full specification: [guide.md](./guide.md). Tool reference:
[reference.md](./reference.md).
