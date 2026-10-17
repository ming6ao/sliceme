# Sliceme directory slices plan

Status: implemented. This document defines one node per directory. It also
records the removal of the GPU field. The engine reads `dir`, rejects a
multi-directory `owns` list, and merges one directory into one node. The
`sliceme.plan` resource runs the bounded planner re-pass loop.

The other documents already describe this target. `README.md`, `guide.md`,
`reference.md`, `workflow.md`, and `architecture.md` name `dir`, the one-visit
rule, and the deepest-owner rule. Earlier, the engine did not implement them. A
plan that followed `guide.md` then passed validation and failed at record time.
Validation accepted a node with an empty directory list, the phase projection
placed every node in one phase, and the recorder reported every changed path as
`stray_path`. The engine now closes that gap.

Read [guide.md](./guide.md) and [reference.md](./reference.md) first. This plan
assumes that model and replaces one part of it: the unit of work.

## 1. Goal

Today one node may own several directories. The scheduler treats the node as the
unit of work. This plan makes the **directory** the unit of work.

The result is three rules:

1. One node owns exactly one directory.
2. One directory appears in exactly one node.
3. One worker subagent runs one node.

A node is therefore a **directory slice**. The slice is the scheduling unit,
the worker unit, and the commit unit.

## 2. Decisions

| Decision | Choice |
|---|---|
| Who splits a multi-directory node | The planner. |
| Who merges two visits of one directory | The planner, in a second pass. |
| The commit unit | One slice: one node, one directory, one commit. |
| The node id | A slug. It matches `[A-Za-z0-9._-]{1,128}`. |
| The `dir` field | The one directory. It is the only ownership source. |
| The node id compared with `dir` | They stay two fields. The id is not the path. |
| The defensive merge | The engine merges nodes that share one directory. It returns `revisited_directory` when the merge would make a cycle. |
| The `gpu` field | Removed. The engine rejects a plan that sets it. |
| The planner re-pass bound | Three passes. A re-pass cannot rewrite a recorded node. |

The planner does the thinking. The engine validates, merges, schedules, records,
and delivers.

The node id must not contain a slash. The worker log file is
`.sliceme/<branch>.worker_<id>.log`. A slash in the id creates a missing parent
directory and breaks the log path. The id is therefore a slug, never a path.

## 3. The model

```text
planner output:  feature nodes, each with one dir
      |
      +-- planner pass 1: merge  one visit per directory
      +-- planner pass 2: order  cross-directory depends_on edges
      v
dag.json:        one node per directory, one directory per node
      |
      v
engine:          validate -> merge -> project phases -> worker -> record -> check
```

## 4. The planner contract

The planner output must satisfy three rules:

- every node declares exactly one directory;
- no directory appears in two nodes;
- the node graph is acyclic.

The planner does the normalizations. The engine validates and rejects a bad
result. The engine never splits a node. The engine does merge nodes that share
one directory, because that merge is safe and mechanical. The engine returns
`revisited_directory` when the merge would make a cycle.

Section 9 defines the split rule and the merge rule in full.

## 5. Split by directory

A node that owns `[src/api, docs]` is not valid. The planner splits it into two
nodes:

- `src/api` with the node goal and acceptance;
- `docs` with the node goal and acceptance.

The two parts share the original dependencies. They have no edge between them,
so they may run in the same phase when the size cap allows.

The engine does not split. It rejects the node with `multi_directory_node`. The
planner must split before the engine sees the plan.

## 6. Merge the revisits

A dependency chain may visit one directory twice.

```text
nodes:       A(src/api) -> B(src/app) -> C(src/api)
```

Node `A` adds a function and a first return type. Node `B` calls the function.
Node `C` refines the return type.

The node graph is acyclic. The directory graph is a cycle, because grouping all
`src/api` nodes into one slice gives `src/api -> src/app -> src/api`.

The planner resolves this in a second pass. It folds `C` into `A`:

```text
A(src/api): add parse() with the final signature
B(src/app): call parse()
A -> B
```

Node `C` does not exist. The planner prefers the **interface-first** order: it
decides the full interface before the caller, then the caller depends on it.

A directory that a genuine second pass must revisit becomes a second campaign.
The design already supports a `sliceme-campaigns` sequence in one design
document. Each campaign then visits each directory once. See
[multi-campaign.md](./multi-campaign.md).

## 7. Order the work

After the split and the merge, the planner adds the cross-directory
`depends_on` edges. The directory graph is now the node graph, and it is
acyclic.

The node that owns a directory also edits that directory's build files. A
shared build file inside `src/api` belongs to the `src/api` node. The one-visit
rule permits only one node for that directory.

Shared build files at the repository root (`BUILD`, `Cargo.toml`, lockfiles) go
to one aggregation node that owns `.`. Every touched part depends on that node.
Use an aggregation node only for the root directory. A per-directory build file
never needs one.

## 8. Schema

One node carries one directory and an ordered task list.

```jsonc
{
  "id": "api",
  "dir": "src/api",
  "label": "the API layer",
  "goal": "Add parse() with the final signature",
  "tasks": ["add the parser", "export it from the package root"],
  "depends_on": ["types"],
  "acceptance": ["pytest tests/api"],
  "no_merge": false
}
```

Change set:

| Old | New | Note |
|---|---|---|
| `owns: ["src/api", "docs"]` | `dir: "src/api"` | one directory per node |
| `owns: ["src/api"]` | `dir: "src/api"` | the one-entry shim |
| `id: "src/api"` | `id: "api"` | the id is a slug, not a path |
| none | `tasks` | the ordered worker prompt |
| `gpu` | removed | the engine rejects it |

Keep a read shim for one release. The shim accepts an `owns` list with one
entry and maps it to `dir`. The engine rejects an `owns` list with two or more
entries with the reason code `multi_directory_node`. The engine also rejects a
node that sets `dir` and `owns` to different directories with
`conflicting_directory`. The engine rejects a node id that does not match
`[A-Za-z0-9._-]{1,128}` with `bad_node_id`.

## 9. Engine validation and reason codes

The engine validates the planner output. It returns one structured reason code
per failure.

| Code | Meaning |
|---|---|
| `multi_directory_node` | A node declares more than one directory. |
| `conflicting_directory` | A node sets `dir` and `owns` to different directories. |
| `bad_node_id` | The node id is not a slug. |
| `revisited_directory` | One directory appears in two nodes, and the engine cannot merge them. |
| `stray_path` | A changed path maps to no node. |
| `out_of_phase_path` | A changed path maps to a node that is not in the current phase. |
| `ambiguous_path` | A changed path maps to more than one node. This is defensive: the one-visit rule makes it unreachable. |
| `unknown_dependency` | A node depends on a node that does not exist. |
| `dependency_cycle` | The node graph has a cycle. |

The engine performs a defensive merge before it validates the one-visit rule.
The merge uses these steps:

1. Group the nodes by directory.
2. Merge each group into one node. The lowest topological index survives.
3. Rewrite every `depends_on` edge that points at an absorbed node.
4. Reject the plan with `revisited_directory` when the merged graph has a cycle.
5. Reject the plan with `revisited_directory` when a node with `no_merge` shares
   a directory with another node.

A node with `no_merge` never merges. A node with recorded progress gets
`no_merge` at projection time, so a defensive merge never rewrites a recorded
node.

## 10. Scheduling

The phase projection keeps its shape. The unit is now the directory.

- Every dependency node lives in an earlier phase.
- Two nodes in one phase must not own equal, ancestor, or descendant directories.
- The phase size is at most `concurrency`, which defaults to 3.

The ancestor rule still holds. When `src` and `src/api` each have a node, they
land in different phases. The rule keeps the recorder exact.

## 11. Conformance: the deepest owner

The recorder maps each changed path to the **deepest** node directory that
contains it, across **all** nodes, not only the current phase. The mapped node
must be a member of the current phase.

```text
changed path      deepest owner    valid when
src/top.py        src              the src node is in this phase
src/api/x.py      src/api          the src/api node is in this phase
docs/guide.md     docs             the docs node is in this phase
```

Today the recorder maps against the current-phase nodes only. A `src` node can
therefore change `src/api/...` when the `src/api` node sits in a later phase.
The deepest-owner rule closes that gap.

A path with no owner is a `stray_path`. A path whose owner is not in the current
phase is an `out_of_phase_path`.

Add one helper to `sliceme/ownership.py`:

```python
def deepest_owner_of(path: str, dirs: dict[str, str | None]) -> str | None:
    """The node id whose directory is the deepest match for *path*, or None."""
```

The helper returns one id or `None`. The one-visit rule makes the result
unambiguous.

## 12. Worker and record

One worker runs one ready node. The changes are small:

- the node owns one directory, so the worker edits one directory;
- the task names the directory and lists the `tasks` in order;
- the worker never runs `git`, never commits, and never runs the test suite;
- the recorder writes one commit per node, and the node is the slice;
- the node id stays a slug, so the worker log path stays flat;
- a node that changes no file records an explicit empty marker.

## 13. The planner re-pass loop

The `sliceme.plan` workflow resource runs the planner and the loop. The
coordinator launches the resource after `start` and before `open`. The resource
lives in `integrations/pi/plan-resource.ts`.

```text
sliceme.plan -> planner -> dag.json -> sliceme ready (validate)
                   ^                          |
                   |      reason code + text  |
                   +--------------------------+
```

The loop:

1. Read the DAG path and the branch names from `sliceme status`.
2. Run `sliceme-planner` with the design and, on a later pass, the last error.
3. Validate the DAG with `sliceme ready`.
4. Return `planned` on success. Run the planner again on a failure.

The bound is three passes. After the third failure the resource returns
`failed` with the last error. The coordinator stops and reports it.

The resource runs at plan time, so no node has a record. A re-pass can
therefore not rewrite a recorded node. The engine still enforces the rule: it
rejects a plan that drops a recorded node, or that leaves a `no_merge` node in a
shared directory, with `revisited_directory`.

## 14. Code impact

| Area | Change |
|---|---|
| `sliceme/ownership.py` | Add `deepest_owner_of` and `parse_dir`. Read `dir` and the `owns` shim in `node_dir`. Enforce the node-id rule. Reject `gpu`. Merge the directory groups in `merge_same_own_nodes`, and raise `revisited_directory` on a cycle or a `no_merge` clash. |
| `sliceme/verbs/campaign.py` | Normalize the DAG before record. Map conformance through `deepest_owner_of` across all nodes. Add the `out_of_phase_path` code. |
| `sliceme/verbs/status.py` | Normalize before `ready`. Report `dag_path`. Reject a plan that drops a recorded node. Keep the phase cursor on the node ids. |
| `sliceme/plan.py` | No change. The `sliceme-campaigns` block carries the campaign `dirs` scope, not the node directory rule. |
| `sliceme/campaign.py` | Read `dir` in the report and the evidence. |
| `integrations/pi/agents/planner.md` | The split pass, the merge pass, the one-visit rule, the slug id, and the root-only aggregation node. Remove `gpu`. |
| `integrations/pi/agents/worker.md` | One directory and an ordered task list. |
| `integrations/pi/plan-resource.ts` | New: the `sliceme.plan` resource. Run the planner, validate through `sliceme ready`, and re-pass with the error text. Bound the loop to three passes. |
| `integrations/pi/coordinator.ts` | Register the `sliceme.plan` resource and launch it before `open`. |
| `integrations/pi/campaign-resource.ts` | Export `engineCommand` for the plan resource. One worker per ready node. |
| Docs | `guide.md` corrected the two examples that set `id` equal to the path. `workflow.md`, `reference.md`, `architecture.md`, and `README.md` name the `sliceme.plan` resource. |

## 15. Tests

Add these tests:

- a two-directory node is rejected with `multi_directory_node`;
- a node with a matching one-entry `owns` shim keeps its directory;
- a node with `dir` and a different `owns` is rejected with
  `conflicting_directory`;
- a node id with a slash is rejected with `bad_node_id`;
- a `gpu` field is rejected;
- the deepest owner wins when `src` and `src/api` both have a node;
- a change to `src/api` from the `src` phase is an `out_of_phase_path`;
- a nested-directory pair lands in two phases;
- two independent nodes that share one directory merge;
- a chain that revisits one directory is rejected with `revisited_directory`;
- a `no_merge` node that shares one directory is rejected with
  `revisited_directory`;
- a plan that drops a recorded node is rejected with `revisited_directory`;
- a directory revisit in a second campaign is allowed;
- the `sliceme.plan` resource stops after the bound of three passes, and it
  never records, checks, approves, opens, or delivers.

## 16. Migration

1. Read `dir` and the one-entry `owns` shim.
2. Reject `owns` with two or more entries.
3. Reject `gpu`. Done.
4. Change the planner prompt and the worker prompt.
5. Change the recorder to the deepest-owner rule.
6. Change the report and the evidence to `dir`.
7. Correct the `guide.md` examples.
8. Deleting the `owns` shim: ship `dir` in version `0.4.0`, accept the one-entry
   `owns` shim in `0.4.x` with a deprecation notice, and delete the shim in
   version `0.5.0`.

## 17. Risks and open decisions

Resolved:

- **The node id and the directory.** They stay two fields. The id is a slug. A
  path id breaks the worker log path. See section 2.
- **The one-entry `owns` shim window.** Read the shim in `0.4.x`. Delete it in
  `0.5.0`. See section 16.

Open:

1. **The planner is stricter.** It must predict the interface early, or split
   the work into two campaigns. The planner prompt must carry the section 6
   example.
2. **The commit unit is coarse.** One directory, one commit. The campaign
   approval covers the size.
3. **A failed task fails the whole slice.** The check was already per phase, so
   the loss is small. The coordinator corrects the `dir` or adds a `depends_on`
   edge, then reruns the node.
4. **The planner prompt must state the merge rule.** A vague prompt produces a
   `revisited_directory` error and a slow re-pass.

## 18. Relation to the GPU removal

The GPU field is removed. The engine rejects a plan that sets it. The scheduler
has no GPU conflict rule, and the sandbox has no GPU runner. The directory
conflict rule is the only scheduling rule. See section 10.
