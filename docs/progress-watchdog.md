# Progress tracking and per-node retry for campaign workers

> **Status.** Proposal. The deliberate gap in `docs/reference.md` §6 says
> Sliceme keeps no per-child progress file. This proposal revises that one
> gap: it adds a progress file per node and a bounded per-node retry. Every
> other rule stands.

The campaign resource starts one phase as one batch and throws on the first
child failure or host failure. This proposal adds middle ground: the
coordinator reads incrementing counters, and the resource retries one node
with a prompt built from its own evidence. The retry keeps sibling work.

## 1. Failures today (`docs/guide.md` §8)

| Failure | Happens today |
|---|---|
| Model call fails in a child | pi auto-retries (3 tries); on a final failure the run ends failed, the whole script throws, and the phase keeps uncommitted edits |
| Tool call fails in a child | the error tool result goes back to the model; the campaign sees nothing; `record` writes an empty marker for a worker that changed no file |
| Engine verb host call fails, or `check` exits 1 for a `failed` verdict | the script throws; the verdict JSON on standard output is lost |
| Host call times out | one hour timeout (`HOST_TIMEOUT_MS`) covers `ready`, `record`, `check`, and `status` |

## 2. Goals and non-goals

| Goals | Non-goals |
|---|---|
| every worker writes incrementing counters to the engine | no per-child progress UI |
| the coordinator sees a stalled child within seconds | no mid-phase commit of a partial phase |
| one failed node retries with a prompt built from evidence | no new budget model; the spend file stays the cost authority |
| the retry never touches sibling work | |

## 3. The progress channel

### 3.1 State

```
.sliceme/
  feat--x.progress/<node>.json     # the last event (one JSON object)
  feat--x.progress/<node>.jsonl    # append-only tail, rotated at a byte cap
```

One event is one JSON line:

```json
{"node": "n7", "step": 4, "name": "edit payments.py", "tool": "edit",
 "ts": 1731234567.5, "detail": "", "run_id": "r-123"}
```

### 3.2 The writer

Background children load the installed sliceme pi package, so its
extension hook fires in each worker. The flow:

```text
child tool result -> extension hook -> append <node>.jsonl
                    (one event)       update <node>.json
```

The launch passes the mapping through `options.extensionBindings`
(`{"sliceme/1": {"campaign": key, "node": id}}`), so the hook reads
`PI_SUBAGENT_EXTENSION_BINDINGS` in `session_start` and knows the campaign
and the node. The model does nothing and spends no tokens on progress.

Two guards keep the channel safe: at most one line per second per child, and
a rotation cap on the tail.

Fallback when the ambient extension does not load: a `sliceme_progress`
write-only tool in the worker agent definition, with one call per completed
task in the prompt.

### 3.3 The reader

- `progress` reports `{node, step, last_ts, stalled_ms, attention}` per node.
  `ready --brief` carries the same fields; and
- `record` reads the counters into the verdict row. `report` shows
  `node n7: 2 tries, 3 stalls, waited 11m` beside the timing statistics.

`resume` reads the same fields, so a suspended campaign restarts with the
correct try count.

## 4. The judge

### 4.1 Per-child deadlines

The resource sets per child (`docs/tool-reference.md`, workflow child
options):

| Option | Role |
|---|---|
| `options.toolTimeoutMs` | one hard deadline per tool call; an open tool past it ends the run with `timedOut: true` |
| `options.timeoutMs` | the run deadline |
| `options.checkpointBeforeDeadlineMs` | the runner steers "checkpoint and stop" before the kill; async single-agent runs |
| `outputSchema` | one explicit `status` field, so a silent no-op child is distinct from a good child |

`Promise.allSettled` is not a documented observe pattern. The script observes
each promise with a catch wrapper and one `Promise.all`:

```js
const jobs = ids.map(id =>
  runs.run("node-" + id, {
    agent: "sliceme-worker", cwd: worktree, task: taskFor(id),
    options: { toolTimeoutMs: TOOL_TIMEOUT_MS, timeoutMs: NODE_TIMEOUT_MS,
               checkpointBeforeDeadlineMs: 180_000,
               outputSchema: RESULT_SCHEMA },
  }).then(result => ({ id, ok: true, result }),
          error => ({ id, ok: false, error }))
);
const settled = await Promise.all(jobs);
```

### 4.2 Stalls

| Signal | Catch |
|---|---|
| `toolTimeoutMs` | one hung command |
| `timeoutMs` | a child that never ends |
| `stallMs` with no counter increment | a child that loops between fast tools |

In phase one the stall path stays read-only: the tick marks a stall and notifies the session;
the child still ends at its own deadline. The tick never aborts a turn.

### 4.3 The coordinator tick

The budget watchdog in `coordinator.ts` already runs a 15-second tick:

```
tick -> read .progress/*.json -> stalled? -> mark and notify the session
```

The wave skips a node the descriptor already marks finished (`recorded`).
It never deletes mirrors, so the frozen mirror of a recorded node grows
stale forever; a stall count for that node would be a phantom in `report`,
`status --dense`, and `resume`.

## 5. The retry loop

The script retries each failed child in place, two tries, like the three
planner passes:

```text
for each failed child:
  if try == 1 and the spend verdict allows:
    relaunch one child with the new key "node-" + id + "-try-2"
       and a salvage prompt
  else:
    block the node: {node, reason, tries}
```

The key must differ from the failed key: one run returns the old receipt for
a reused key (`docs/workflows.md`). A node that fails again returns `state:
"blocked"` for that node only; the loop records the phase and the return
names the node, the reason, and the try count.

*Implementation correction (resolved with the plan's owner):* the reset
travels with the salvage child, not with a host call. The pi-subagents host
authority authorizes a granted `runs.host` command only as an exact string,
and a fixed command carries no variable `--node` argument. So the script
cannot call `sliceme recover --node <id>` itself. The salvage prompt carries
the reset as one directive: the child reads its own bounded progress tail,
then runs `git restore --source=HEAD -- <owned dir>` for its own directory.
The `recover` engine verb stays exactly as specced here for the engine path
(the coordinator tool, trusted callers, and tests). The script grants
nothing for it, and the grant count stays 3 per phase plus the final
`status` grant.

The salvage prompt comes from evidence:

- the terminal reason: `timedOut`, `blocked`, or the model error text; and
- one directive keyed to the reason, for example: "your build command hung;
  run it with a short timeout first".

The child reads the progress tail itself, from the progress directory path
the `ready` brief reply carries, so the tail no longer rides the prompt
text. The reply names one relative path per phase; the tail file is
`<node>.jsonl` beside it. A node that declares no directory carries no
reset directive: it owns no partial work to restore, and a reset spanning
the worktree root would destroy every sibling's work.

### 5.1 The `recover` verb (replaces `reset_node`)

One verb serves the evidence read and the single-node reset on the engine
path:

```bash
sliceme recover --node ID
    print the node progress tail as JSON, then
    git restore --source=HEAD -- <owned dir>
```

Ownership in a phase is disjoint, so the restore touches one node directory.
The verb waits until the run is terminal, and it keeps the workspace lock
like `record`, so no record and no check interleaves with the restore. The
salvage child resets its own directory with the same command shape, ordered
by the one salvage directive.

## 6. Caps and costs

| Cap | Effect |
|---|---|
| 32 authority grants | 3 grants per phase plus 1 for `status`: `phaseCap` stays 10 (30 + 1) |
| the spend file | the retry reads the spend verdict through `ready`, so a campaign near its cap runs one try, not two |
| `EVIDENCE_LIMIT` | the progress tail and the salvage prompt stay bounded |
| `record` | the engine derives the per-node try count from the tail's `run_id` values; the host authority lets no dynamic node argument reach a granted command |

## 7. State and ownership

| File | Writer | Reader |
|---|---|---|
| `.progress/*.json` and `.jsonl` | the child extension hook | the engine, the tick |
| `state.db` records | `record` with per-node try counts passed by the resource | `status`, `report`, `resume` |
| the descriptor `.session.json` | the pi adapter only (`docs/reference.md` §3) | `resume`, `sessions` |

The workflow sandbox has no filesystem access, so the script cannot write the
descriptor or the counters. It passes the try counts to `record`, and the
engine owns every stored write.

## 8. Implementation slices

| Slice | Content | Engine change | Grant change |
|---|---|---|---|
| 1 | progress channel: hook, files, reader | `progress` verb, `ready` fields | none |
| 2 | structured child results; try counts at `record` | record columns | none |
| 3 | per-node retry loop in the resource | none | none |
| 4 | `recover` verb | new verb | none (engine path; see §5) |
| 5 | stall wave in the coordinator tick | none | none |

Slices 1 to 5 carry no grant change and ship in this order.

## 9. Tests

| Area | Test |
|---|---|
| progress channel | one line per second, rotation, mirror update |
| reader verbs | `progress` output shape, `ready --brief` fields |
| retry loop | one relaunch with a new key, then `blocked` |
| `recover` | the restore touches one node dir; the tail rides the reply |
| try counts | `record` stores them; `resume` reconciles them |
| salvage prompt | the tail and the terminal reason ride the prompt |
| e2e | a stalled child does not block sibling nodes |
