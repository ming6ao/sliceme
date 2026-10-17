# Sliceme directory slices plan

Status: proposed. This document defines one node per directory. It also records
the removal of the GPU field. The engine change is not implemented yet, so the
code still accepts a multi-directory `owns` list. The GPU removal is
implemented.

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
| The `gpu` field | Removed. The engine rejects a plan that sets it. |

The planner does the thinking. The engine validates, schedules, records, and
delivers.

## 3. The model

```text
planner output:  feature nodes, each with owns = [dir, ...]
      |
      +-- planner pass 1: split  one node per directory
      +-- planner pass 2: merge  one visit per directory
      +-- planner pass 3: order  cross-directory depends_on edges
      v
dag.json:        one node per directory, one directory per node
      |
      v
engine:          validate -> project phases -> worker -> record -> check
```

## 4. The planner contract

The planner output must satisfy three rules:

- every node declares exactly one directory;
- no directory appears in two nodes;
- the node graph is acyclic.

The planner does both normalizations. The engine only validates the result.

## 5. Pass 1: split by directory

A node that owns `[src/api, docs]` becomes two nodes:

- `src/api` with the node goal and acceptance;
- `docs` with the node goal and acceptance.

The two parts share the original dependencies. They have no edge between them,
so they may run in the same phase when the size cap allows.

## 6. Pass 2: merge the revisits

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

## 7. Pass 3: order the work

After the split and the merge, the planner adds the cross-directory
`depends_on` edges on the merged graph. The directory graph is now the node
graph, and it is acyclic.

Shared build files (`BUILD`, `Cargo.toml`, lockfiles) go to one aggregation
node that owns the shared directory. Every touched part depends on that node.

## 8. Schema

One node carries one directory and an ordered task list.

```jsonc
{
  "id": "src/api",
  "dir": "src/api",
  "label": "the API layer",
  "goal": "Add parse() with the final signature",
  "tasks": ["add the parser", "export it from the package root"],
  "depends_on": ["src/types"],
  "acceptance": ["pytest tests/api"],
  "no_merge": false
}
```

Change set:

| Old | New | Note |
|---|---|---|
| `owns: ["src/api", "docs"]` | `dir: "src/api"` | one directory |
| `goal` | `goal` | unchanged |
| none | `tasks` | the ordered worker prompt |
| `gpu` | removed | the engine rejects it |

Keep a read shim for one release. The shim accepts an `owns` list with one
entry and maps it to `dir`. The engine rejects an `owns` list with two or more
entries with the reason code `multi_directory_node`.

## 9. Engine validation and reason codes

The engine validates the planner output. It returns one structured reason code
per failure.

| Code | Meaning |
|---|---|
| `multi_directory_node` | A node declares more than one directory. |
| `revisited_directory` | One directory appears in two nodes. |
| `stray_path` | A changed path maps to no node. |
| `ambiguous_path` | A changed path maps to more than one node. |
| `unknown_dependency` | A node depends on a node that does not exist. |
| `dependency_cycle` | The node graph has a cycle. |

The engine also performs a defensive merge. When two nodes share one directory
and the merge keeps the graph acyclic, the engine merges them. Otherwise it
returns `revisited_directory` and the coordinator runs the planner again.

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

Add one helper to `sliceme/ownership.py`:

```python
def deepest_owner_of(path: str, owners: dict[str, list[str]]) -> list[str]:
    """The node ids whose directory is the deepest match for *path*."""
```

## 12. Worker and record

One worker runs one ready node. The changes are small:

- the node owns one directory, so the worker edits one directory;
- the task names the directory and lists the `tasks` in order;
- the worker never runs `git`, never commits, and never runs the test suite;
- the recorder writes one commit per node, and the node is the slice;
- a node that changes no file records an explicit empty marker.

## 13. The planner re-pass loop

The engine returns a plan error to the coordinator. The coordinator runs the
planner again with the error text. The planner rewrites `dag.json`. The engine
validates again.

```text
coordinator -> planner -> dag.json -> engine validate
                  ^                       |
                  |   reason code + detail |
                  +-----------------------+
```

Bound the passes to three. Then stop with the last error.

## 14. Code impact

| Area | Change |
|---|---|
| `sliceme/ownership.py` | Add `deepest_owner_of`. Add `split_multi_dir_nodes` and `merge_directory_nodes` for the defensive path. Read `dir` and the `owns` shim. Reject `gpu`. |
| `sliceme/verbs/campaign.py` | Record one commit per node. Map conformance through `deepest_owner_of`. |
| `sliceme/verbs/status.py` | The phase cursor and `ready` use the directory node ids. |
| `sliceme/plan.py` | Validate the one-directory rule and the one-visit rule. |
| `sliceme/campaign.py` | Read `dir` in the report and the evidence. |
| `integrations/pi/agents/planner.md` | The split pass, the merge pass, and the one-visit rule. Remove `gpu`. |
| `integrations/pi/agents/worker.md` | One directory and an ordered task list. Remove the GPU rule. |
| `integrations/pi/campaign-resource.ts` | No change: one worker per ready node. |
| Docs | This plan, `guide.md`, `reference.md`, `workflow.md`, and `architecture.md`. |

## 15. Tests

Add these tests:

- a two-directory node splits into two nodes;
- a plain node keeps its one directory and its `tasks`;
- a revisited directory is merged by the planner, or rejected with
  `revisited_directory`;
- a `gpu` field is rejected;
- an `owns` list with two entries is rejected with `multi_directory_node`;
- the deepest owner wins when `src` and `src/api` both have a node;
- a change to `src/api` from the `src` phase is a `stray_path`;
- the planner re-pass loop stops after the bound;
- a nested-directory pair lands in two phases.

## 16. Migration

1. Read `dir` and the one-entry `owns` shim.
2. Reject `owns` with two or more entries.
3. Reject `gpu`.
4. Change the planner and the worker prompts.
5. Change the recorder to the deepest-owner rule.
6. Change the phase cursor to the directory ids.
7. Update the docs.
8. Delete the `owns` shim after one release.

## 17. Risks and open decisions

1. **The planner is stricter.** It must predict the interface early, or split
   the work into two campaigns.
2. **The commit unit is coarse.** One directory, one commit.
3. **A failed task fails the whole slice.** The check was already per phase, so
   the loss is small.
4. **The planner prompt must state the merge rule.** A vague prompt produces a
   `revisited_directory` error and a slow re-pass.
5. **The node id and the directory.** They may be the same value. Keep them as
   two fields so a future label can differ from the path.
6. **The one-entry `owns` shim window.** Confirm when the shim can be deleted.

## 18. Relation to the GPU removal

The GPU field is removed before this plan lands. The scheduler no longer has a
GPU conflict rule, and the sandbox no longer has a GPU runner. The directory
conflict rule is the only scheduling rule. See section 10.
