# Sliceme terminology and simplification plan

> **Superseded.** This is a historical plan. The current target design is
> [directory-slices-plan.md](./directory-slices-plan.md).
>
> **Naming note.** The word `phase` below means the retired node field. The
> word `wave` was the old name for the scheduling unit, now called the phase.

Status: proposal (historical).

This document explains the current concepts of Sliceme. It proposes one smaller
vocabulary and one smaller design. The plan keeps the behavior that works. The
plan removes names that duplicate other names and structures that duplicate
other structures.

Read [reference.md](./reference.md) and [guide.md](./guide.md) first. This plan
assumes that model.

## 1. What the code does today

Sliceme turns a design document into a graph of work. The engine derives phases
from the graph. One workflow resource runs one worker per ready node. The engine
records each phase as a set of commits.

The engine then checks the combined tree and writes one verdict. A human
approves the campaign one time. The engine pushes the work branch and opens one
pull request.

The engine is dependency-free Python. The adapter is TypeScript for pi. The
engine owns all state. The adapter parses arguments and renders results.

## 2. The concepts and abstractions

### 2.1 The actors

| Actor | Where | Job |
|---|---|---|
| **Coordinator** | the top-level pi session | owns the plan, starts the resource, records the approval, runs delivery |
| **Planner** | a pi subagent | reads the design, writes `dag.json` |
| **Worker** | a pi subagent per node | edits owned directories only, then stops |
| **Reviewer** | the builtin pi reviewer | reads the recorded evidence, never edits |
| **Resource** | `sliceme.campaign` | runs the loop: ready, spawn, record, check, review |
| **Engine** | `sliceme/` Python | owns state, isolation, checks, and delivery |

### 2.2 The engine nouns

| Term | Meaning in the code |
|---|---|
| **Plane** | one repository root with a `.sliceme/` directory. It holds one SQLite database, one lock, and the repository default branch. |
| **Campaign** | one row in `campaigns`. It holds one work branch, one base branch, one design, and one DAG. |
| **Unit** | one row in `units`. It holds one worktree and one branch. Kinds: `worker` and `campaign`. |
| **Node** | one item of work in the DAG. Fields: `id`, `label`, `goal`, `owns`, `depends_on`, `acceptance`, `gpu`, `phase`, `no_merge`. |
| **Owns** | the repository-relative directories that one node may change. The engine compares directories by subtree. |
| **Phase** | a derived batch of nodes with disjoint `owns`. It is also one record and check checkpoint. The engine derives the phases from the DAG. |
| **Candidate** | one row in `candidates`. It is a node commit that waits for delivery. |
| **Check** | one row in `checks`. It holds one terminal verdict for one fingerprint. |
| **Fingerprint** | one hash of tree, commands, toolchain, policy, sandbox, check version, and source. |
| **Review decision** | one row in `review_decisions`. One campaign approval. |
| **Evidence** | `.sliceme/<key>.evidence.json` and `.evidence.md`. The Markdown document is the pull request body. |
| **Report** | `.sliceme/<key>.report.md`. The deterministic report. |
| **Session descriptor** | `.sliceme/<key>.session.json`. The adapter writes it for suspend and resume. |
| **Control file** | `.sliceme/<key>.control.json`. The cooperative pause flag. |
| **Sandbox** | one isolation profile from a project manifest. The digest is part of the fingerprint. |

### 2.3 The branch names

The code uses four names for one branch and three names for another branch.

| Name | Same value | Meaning |
|---|---|---|
| `feature_branch` | the work branch | the pull request head, for example `feat/my-design` |
| `target_branch` | the work branch | a legacy name |
| `main_branch` | the work branch | a misleading legacy name |
| `worktree_branch` | the work branch | the branch of the campaign worktree |
| `delivery_base` | the base branch | the pull request base, for example `main` |
| `base` | the base branch | a short legacy name |
| `default_branch` | the protected branch | the repository default branch; it stays separate when `--base` overrides the base |

`config.json` and the `campaigns` table hold every name at once. The docs call
the value the "campaign branch" and the "delivery base". The code and the docs
therefore disagree on the words.

The base branch and the protected branch are the same value only until a caller
passes `--base`. With `--base release`, the base branch is `release` and the
protected branch is still `main`. That difference is the delivery guard.

### 2.4 The lifecycle

```text
start      create the plane and the campaign; derive the work branch
plan       parse the design's campaign split
phase --open    fetch the base branch; create the worktree from origin/<base>
loop
  ready          ready nodes of the current phase
  runs.run       one worker per ready node
  phase --record  one commit per node; enforce ownership conformance
  check          one combined-tree verdict
  runs.run       one reviewer over the recorded evidence
evidence   write the deterministic evidence document
report     write the deterministic report
approve    record the one campaign approval
deliver    push the work branch; open one pull request against the base branch
```

## 3. Naming problems

1. **One branch has four names.** `feature_branch`, `target_branch`,
   `main_branch`, and `worktree_branch` denote one branch. `main_branch` is the
   worst name, because the value is never `main`.
2. **One base has two names.** `delivery_base` and `base` denote the pull
   request base. `default_branch` is a separate protected value, and the plan
   keeps it separate.
3. **`unit` is a dead abstraction.** It once meant any worktree. Now the
   campaign owns one unit. The name competes with the DAG unit of work.
4. **`candidate` is vague.** It means "a node commit". The code never counts
   candidates in a meaningful way.
5. **`phase` is dead.** It is display-only. No rule reads it.
6. **The phase is both a hint and a gate.** The docs say that readiness is the
   gate and that the phase is a display hint. The code scopes `ready`, `record`,
   and `check` to the current phase. The two statements contradict.
7. **`check` carries too many meanings.** The word is a verb, a table, a
   runner, a config list, and a command.
8. **`campaign` carries too many meanings.** The word is a row, a branch, a
   DAG, a loop, and a workflow resource.
9. **The `dir:` prefix is redundant.** The engine already rejects every
   non-directory kind. A bare directory path carries the same meaning.
10. **`state.json` duplicates the database.** The engine never writes it. The
    reader merges two state sources. The merge adds code to `ready`, `status`,
    and the phase projection.

## 4. Target vocabulary

The plan keeps the product name and the actor names. The plan replaces the
ambiguous nouns and the duplicate branch names.

| Old | New | Note |
|---|---|---|
| campaign branch, `feature_branch`, `target_branch`, `main_branch`, `worktree_branch` | **work branch**, `work_branch` | one branch, one name |
| delivery base, `delivery_base`, `base` | **base branch**, `base_branch` | the pull request base |
| `default_branch` | **protected branch**, `protected_branch` | keep separate; it is not the base (see the note below) |
| unit | *(delete)* | the campaign owns the worktree |
| candidate | **record** | one node commit |
| `prepared` status | **recorded** | |
| `landed` status | **delivered** | |
| check row | **verdict** | the verb stays `check` |
| review decision | **approval** | the verb stays `approve` |
| node `done` | **recorded** | a recorded node satisfies a dependency |
| `phase` | *(delete)* | no rule reads it |
| `dir:src/api` | `src/api` | directories only |

**Keep the protected branch separate from the base branch.** The code writes
`default_branch` and `delivery_base` as different values in
`sliceme/verbs/bootstrap.py`. With `--base release`, the delivery base is
`release` and the default branch stays `main`. The guard `is_default_branch`
in `sliceme/integrate.py` reads `config["default_branch"]`. A single
`base_branch` name would let `--base` overwrite the protected value and weaken
the guard. Rename `default_branch` only if the amount of churn is worth it, or
keep the name.

Final engine names:

- `plane` (or `workspace`): the `.sliceme/` root. The oracle review found the
  `workspace` rename is pure churn, so this plan drops it.
- `campaign`: one work branch, one base branch, one design, one plan.
- `plan`: the DAG and its campaign fields. `dag.json` stays the file name.
- `node`, `owns`, `depends_on`, `acceptance`, `gpu`, `no_merge`.
- `phase`: one derived batch of nodes with disjoint `owns`.
- `record`: one node commit. The table is `records`.
- `verdict`: one check result. The table is `verdicts`.
- `approval`: one campaign approval. The table is `approvals`.
- `work_branch`, `base_branch`, `protected_branch`.
- `evidence`, `report`, `sandbox`, `fingerprint`.

Final verbs (one meaning each):

| Verb | Replaces |
|---|---|
| `start` | `start` |
| `status` | `status` |
| `plan` | `plan` |
| `ready` | `ready` |
| `open` | `phase --open` |
| `record` | `phase --record --current` |
| `check` | `check --current` |
| `approve` | `review --decision approve` |
| `report` | `review --report` |
| `evidence` | `evidence` |
| `deliver` | `deliver` |

The explicit verbs remove the flag combinations from a host grant. Each grant
now binds one verb and one command.

## 5. Rename mechanics

Do the renames in stages. Keep the test suite green after each stage.

### Stage 1: the branch names

1. Add `work_branch` and `base_branch` to `config.json` and to `campaigns`.
2. Keep `default_branch` as its own key. Do not fold it into `base_branch`.
3. Keep a read-time shim that maps the old names to the new names for one
   release.
4. Change every writer to the new names.
5. Change the adapter (`common.ts`, `coordinator.ts`, `campaign-resource.ts`).
6. Change the docs.
7. Delete the shim after one release.

### Stage 2: the state vocabulary

1. Rename the table `candidates` to `records` and the statuses to `recorded`
   and `delivered`.
2. Rename the table `checks` to `verdicts`.
3. Rename the table `review_decisions` to `approvals`.
4. Rename the service methods (`create_candidate` to `create_record`,
   `list_candidates` to `list_records`, and so on).
5. Change the tests and the docs.

### Stage 3: the verbs

1. Split `phase` into `open` and `record`.
2. Split `review` into `approve` and `report`.
3. Delete the `--current` flag. `check` and `record` always act on the current
   phase.
4. Update `surface.ACTIONS`, the `_HANDLERS` table, the CLI, and the adapter.
5. Update the workflow resource grants and its tests.

### Stage 4: the DAG fields

1. Delete `phase` from the schema, the report, and the docs.
2. Delete the `dir:` prefix. Accept only repository-relative directories.
3. Update the planner prompt, the tests, and the docs.

## 6. Structural simplifications

### S0. Keep the phase, and define it once

The phase stays. It is the one scheduling concept after this plan. Delete the
`phase` verb and the "display hint" language instead.

A phase is one scheduling batch and its record and check checkpoint. A batch
obeys four rules:

- every dependency finished;
- the `owns` subtrees do not overlap;
- the member count is at most `concurrency`;
- at most one node needs a GPU.

Delete the precomputed phase only if you accept runtime scheduling. The engine
would then compute the batch in `ready` from current state. That moves the same
three rules into the run path and breaks the plan-time rule in
`docs/workflow.md` hard rule 4. The plan keeps the fixed projection.

Keep the noun. Delete the verb and the prose.

| Item | Action |
|---|---|
| `phase` as a domain noun | keep; define it once |
| `phase` verb | delete; split into `open` and `record` |
| `phase --open` | delete the name; the worktree is per campaign |
| "phase is a display hint" prose | delete from every document |
| `phase` | delete; it is the dead term |
| `_current_phase` / `_recorded_phase` | keep the concept; unify the two functions |
| `checks.phase` column | keep; it names the checkpoint |
| `concurrency` | keep; it is the phase size cap |

### S1. Make the phase the one gate, and unify the phase cursor

The code scopes `ready`, `record`, and `check` to the current phase, but the docs
claim that readiness is the gate and that the phase is a hint. Choose the phase.
Delete the "display hint" language from every document.

The oracle review found that the plan under-specified this change. Two functions
derive the cursor today: `_current_phase` in `sliceme/verbs/status.py` drives
`ready` and `record`, and `_recorded_phase` in the same file drives `check`. The
change must unify them into one cursor.

The oracle also found a stall. A phase member that changes no file gets no commit,
because `record_phase` skips an empty path list at `sliceme/verbs/campaign.py`
line 334. `_phase_rows` marks a phase `done` only when every member has a recorded
commit. `_current_phase` then never advances, so `ready` keeps returning that
member until the resource cap stops the loop.

1. Unify `_current_phase` and `_recorded_phase` into one cursor.
2. Advance the phase when a member records zero changes. Add an explicit
   "recorded empty" marker for the member.
3. Delete the readiness filter in `ready`, `_readiness_state`, and `readiness`.
   A dependency cannot live in the same phase, because `plan_dag_phases` sets
   `min_phase = max(phase(dep) + 1)`. The filter is therefore redundant inside
   one phase.

### S2. Delete the `units` table

The campaign owns one worktree. The `units` table holds one row for that
worktree and one row for each legacy `worker` unit. Only the campaign row is in
use. The oracle review found that the table is more load-bearing than a simple
deletion suggests, so split the work into three steps.

**S2a. Add the worktree to the campaign.**

1. Add `worktree` to `campaigns`.
2. Backfill each row from its campaign unit.
3. Change the worktree readers to `campaigns.worktree`.

**S2b. Move the unit state.**

1. Move `units.state` onto `campaigns`.
2. Change `gc` in `sliceme/verbs/delivery.py` to read the campaign state.
3. Change the report in `sliceme/campaign.py` to read `campaigns`.

**S2c. Drop the unit identity.**

1. Drop `candidates.unit_id` and the `units` foreign key.
2. Rewrite the join in `get_candidate` and `list_candidates` in
   `sliceme/store.py`. Both joins drop the unit columns.
3. Change `_mark_delivered` in `sliceme/integrate.py` to stop calling
   `get_unit` and `set_unit_state`.
4. Change `sliceme/verbs/sessions.py` to locate the worktree through
   `campaigns.worktree`.
5. Delete `units`, `create_unit`, `get_unit`, `set_unit_state`, and
   `_project_unit`.
6. Delete `--name`, `--kind`, and `--no-unit` from `start`.

Each step keeps the test suite green.

### S3. Delete `--only`, `--messages`, and `--summary`

The workflow resource never passes these flags. The commit subject falls back to
the node `label`. The `--only` path adds the `targeted`, `reporter`, and
`phase_order` branches to `record_phase`.

1. Delete the three flags from `surface.ACTIONS` and the adapter.
2. Delete the scoped branch from `record_phase`.
3. Delete the `tests/test_cli.py` cases for `--only`.

### S4. Delete `state.json`

The engine never writes it. Readiness already derives from the recorded commits.
The adapter writes node statuses into the session descriptor. The oracle review
found that this is a contract change, not dead-code deletion: eleven call sites
read the file, and six test files write it.

1. Fold the node-status map and the `attempts` counter into the session
   descriptor. `sliceme/verbs/sessions.py` reads `entry.get("attempts")` today,
   so the `respawn` branch goes dead when the descriptor has no counter.
2. Delete `state_path`, `load_state`, and every merge in `_readiness_state`,
   `_phase_rows`, `_current_phase`, and `sliceme/verbs/sessions.py`.
3. Rewrite the test files that write `state.json`:
   `tests/test_cli.py`, `tests/test_sessions.py`, `tests/test_status_summary.py`,
   `tests/test_campaign.py`, `tests/test_campaigns.py`, and
   `tests/test_merge.py`.
4. Do not add a legacy reader. The engine never wrote `state.json`; only the
   adapter did. The session descriptor is the only node-status source now.

### S5. Collapse the node statuses

The node statuses are `pending`, `running`, `recorded`, `done`, `failed`, and
`paused`. The engine sets `done` when the phase check passes. The phase check is a
combined-tree check, so one node cannot pass alone.

1. Keep `pending`, `running`, `recorded`, and `failed`.
2. Let `recorded` satisfy a dependency.
3. Move pass and fail to the phase verdict.

### S6. Delete one lock file

`campaigns.lock` and `review.lock` both serialize git mutation in one workspace.
The check runner is synchronous. One lock is enough.

1. Keep one `workspace.lock` for campaign creation, record, and delivery.
2. Delete `_delivery_lock`.

### S7. Merge the small helpers

1. Move `_record_error` and `_changed_entries` into `gitutil.py`.
2. Move `_owners_of` into `ownership.py`.
3. Delete `verbs/support.py` when it is empty.

### Cut from the plan

The oracle review proposed two cuts. This plan accepts both.

- **Do not generate the adapter schema.** A `schema` verb plus runtime TypeBox
  construction adds machinery and a new engine-to-adapter contract. Keep the
  explicit `ACTION_FLAGS` list. Generate it later only if it drifts.
- **Do not rename `plane` to `workspace`.** The rename touches about two
  hundred references and adds no behavior.

## 7. Order and verification

Do the structural work before the cosmetic renames. The renames then touch less
code.

Recommended order:

1. S0, S1, S3, S5 (the phase decision, behavior, and dead flags).
2. S2a, S2b, S2c (the unit teardown).
3. S4, S6 (the state model and the lock).
4. Stage 1 and Stage 2 (branch and table names).
5. Stage 3 (verbs).
6. Stage 4, S7 (fields and helpers).

Verification after each step:

```bash
npm test
```

Add these tests:

- one test that one cursor drives `ready`, `record`, and `check`;
- one test that a phase advances when a member records zero changes;
- one test that a recorded node satisfies a dependency;
- one test that one `workspace.lock` serializes record and delivery;
- one test that the resource grants one verb per command.

## 8. Risks and open decisions

1. **Is a `--base` override still in scope?** With `--base release`, the base
   branch is `release` and the protected branch is `main`. Keep the two names
   apart. This plan keeps them apart.
2. **Must a phase advance when a member records zero changes?** If yes, the
   cursor needs an explicit per-member marker. This plan adds one.
3. **The `units` teardown is the break step.** Split it into S2a, S2b, and S2c,
   and run the tests after each step.
4. **A legacy `worker` unit may still matter.** Confirm that no operator uses a
   non-campaign unit. If one does, keep the `units` table.
5. **A stored `state.json` may exist.** The engine ignores it now. A plane
   suspended before this change loses the node statuses that only `state.json`
   carried. This plan accepts that loss, because the engine never wrote the
   file.
6. **The host grant list grows.** The explicit verbs add three keys to the
   resource. Keep every key fixed and reject unknown arguments.
7. **The phase cursor must survive a crash.** The cursor already derives from the
   recorded commits. Test the rebuild path after S1 and S4.
8. **The phase stays.** The plan keeps the phase as the one scheduling concept.
   The plan deletes the `phase` verb and the "display hint" prose instead. See
   S0.
