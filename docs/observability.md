# Sliceme observability design

Status: partly implemented. The durable half landed with `docs/sessions.md`:

- the `attempts` table and the `attempt --begin`/`--end` engine action
  (`sliceme/store.py`, `sliceme/surface.py`, `sliceme/service.py`);
- debounced per-node heartbeat files and the stream reducer in
  `integrations/pi/common.ts::runSubagent`;
- the `runTracked` wrapper that records one try and a heartbeat for the
  planner, each worker, and each verifier
  (`integrations/pi/coordinator.ts`).

The live multi-subagent view (Priority 0) also landed:

- the pure `renderProgress`/`renderAgentLine` renderer in `common.ts`;
- one in-process progress registry and one render timer in the coordinator;
- a per-event `onProgress` feed and a `spawn` row streamed through `onUpdate`.

The state-write race also landed:

- one in-process `CampaignStateStore` per campaign branch owns `state.json` and
  flushes it atomically, so parallel spawn completions keep their node status.

Still proposed: the `progress` action and durable stalled detection
(Priority 1), and the campaign-economics polish (Priority 2).

This document describes how to make a running campaign observable. The primary
goal is a **live progress display for the parallel worker subagents**. A second
goal is a durable, queryable projection, so a second terminal or a finished
report shows the same numbers.

## 1. Goals and non-goals

### Goals

While a campaign runs, answer these questions from the coordinator session and
from a second terminal:

1. How many waves and nodes exist, how many finished, and which wave is current?
2. Which work units run, wait, finish, or fail?
3. For each running subagent: what does it do now, and for how long?
4. What are the aggregate and per-agent costs (turns, tool calls, tokens, cost)?
5. What are the timings (wall clock, queue wait, verification, lead time, ETA)?

### Non-goals

- A long-lived daemon or a second terminal renderer (`reference.md` §6). The
  live view is an in-process renderer plus small on-disk snapshots.
- Precise billing. Token and cost figures are approximate rollups of the
  provider usage.
- Replacing the pi TUI. The display is a `ctx.ui.setWidget` region and the
  per-tool `onUpdate` stream.

## 2. What already exists

- **Wave projection is deterministic and complete.** `plan_dag_waves` produces
  `DagWave(index, members, conflicts)`, which `status` surfaces as `dag_waves`
  and the coordinator reconciles into `state.waves`.
- **Some timing is durable.** `units.created_at`/`updated_at`,
  `candidates.created_at`/`updated_at`, and `jobs` (`requested_at`,
  `started_at`, `finished_at`, `duration`, `exit_code`) exist.
- **The `attempts` table is durable.** It records one planner, worker, or
  verifier run: `started_at`, `finished_at`, `duration`, `exit_code`, `turns`,
  `tool_calls`, `tools`, `tokens_in`, `tokens_out`, `cost`, `last_tool`,
  `last_activity_at`, and `error`. The `attempt --begin`/`--end` action writes
  it.
- **A live activity signal exists.** While a subagent runs, `runSubagent`
  reduces `turn_start`, `tool_execution_start`/`tool_execution_end`,
  `message_update.usage`, and `message_end` into a `SubagentProgress` snapshot.
  It flushes the snapshot at most once per second to
  `.sliceme/<branch-key>.progress_<node>.json`.
- **Executor queue state is available** from `Store.job_counts()` and
  `Executor.status()`.
- **Per-worker logs hold the full `pi --mode json` stream.**
- **Parallel tool calls work.** Pi can run several tool calls from one
  assistant message at the same time, so the `spawn` calls of a wave already
  overlap.

## 3. Gaps

1. **Live updates landed.** The coordinator now owns one progress registry and
   one render timer. The timer recomposes the widget at about 4 Hz, so the view
   stays current while a worker runs.
2. **A live consumer landed.** `runTracked` registers each agent and folds the
   `onProgress` snapshots into it. `spawn` also streams its own row through
   `onUpdate`.
3. **No combined projection.** Nothing joins work-unit status, wave status,
   timings, executor state, agent metrics, and heartbeats into one durable
   snapshot. The proposed `progress` action would do that.
4. **One state store landed.** `state.json` still has no timestamps, but one
   in-process `CampaignStateStore` now owns the file. Parallel spawn
   completions share one state object, so they no longer lose updates (see §9,
   suggestion 2).
5. **Durable stalled detection is not implemented.** A dead worker heartbeat
   simply stops. The live widget marks a silent row `⚠ stalled` from its age,
   but the durable projection has no `stalled` field yet.

## 4. Design principles

- **The engine owns durable state; the adapter owns the live view.** Durable
  numbers come from SQLite and the heartbeat files through a `progress` action.
  The animated view derives from the same reducer.
- **One renderer, one registry.** Exactly one part in the coordinator
  process composes the widget.
- **One event vocabulary.** Reduce the pi stream once into a small event set,
  and share the reducer between the live view and the durable writer.
- **The renderer is pure.** `renderProgress(snapshot, options) -> string[]`
  does no I/O, so a unit test can drive it.
- **Everything is reconstructable** from `.sliceme/` plus git after a crash.
- **Bound the cost.** Debounce heartbeats, use one render timer, and never write
  a file per event or run a subprocess per frame.

## 5. Priorities

**Priority 0 — the live multi-subagent view (no persistence required).**

| # | Feature | Status | Where |
|---|---|---|---|
| P0.1 | Normalized progress events from the `pi --mode json` stream | Implemented: `runSubagent` reduces into a `SubagentProgress` snapshot and emits it per event | `common.ts::runSubagent` |
| P0.2 | One in-process progress registry for all running subagents | Implemented (`liveAgents`) | coordinator extension |
| P0.3 | One render timer (~4–10 Hz) that composes the widget | Implemented (`LIVE_RENDER_MS = 250`) | coordinator extension |
| P0.4 | Per-subagent row: state, node, current tool and argument, elapsed | Implemented (`renderAgentLine`) | `common.ts`, coordinator extension |
| P0.5 | Campaign aggregate line: done/total, wave k/n, elapsed, totals | Implemented | `common.ts::renderProgress` |
| P0.6 | `spawn` streams its own row through `onUpdate` | Implemented | coordinator extension |
| P0.7 | Width-safe, theme-aware string renderer | Implemented (`renderProgress`) | `common.ts` |

**Priority 1 — durable and queryable (crash-safe, second terminal).**

| # | Feature | Status | Where |
|---|---|---|---|
| P1.1 | Debounced per-node heartbeat snapshot file | Implemented | `common.ts` and `.sliceme/` |
| P1.2 | `attempts` table and `attempt --begin`/`--end` | Implemented | `store.py`, `surface.py`, `service.py` |
| P1.3 | `progress` action (`sliceme progress`) | Proposed | `surface.py`, `service.py` |
| P1.4 | Stalled detection | Proposed | `service.py` |
| P1.5 | New actions in `SLICEME_ACTIONS` | `attempt` landed; `progress` proposed | `unit.ts` |

**Priority 2 — polish and campaign economics.**

| # | Feature | Status | Where |
|---|---|---|---|
| P2.1 | ETA and throughput rates (tokens/min, tools/min) | Proposed | `service.py`, renderer |
| P2.2 | Wave durations, node lead time, critical-path estimate | Proposed | `service.py` |
| P2.3 | Tool histogram, verification pass rate, cache hits | Proposed | `progress`, renderer |
| P2.4 | Executor and verification section in the widget | Proposed | coordinator extension |
| P2.5 | Metrics in the report skeleton | Proposed | `campaign.py` |

## 6. Durable heartbeat files (implemented)

For each running node, `runSubagent` writes
`.sliceme/<branch-key>.progress_<node>.json` atomically. It writes at most once
per second plus one final flush, and it uses the `.sliceme/` snake_case
convention:

```json
{
  "node": "w1",
  "unit": "w1",
  "attempt": 1,
  "agent": "worker",
  "pid": 12345,
  "started_at": 1733234401.0,
  "updated_at": 1733234463.2,
  "turns": 7,
  "tool_calls": 23,
  "tools": {"edit": 8, "bash": 6, "read": 9},
  "last_tool": "bash",
  "last_tool_args": "cargo test",
  "last_text": "Running the CPU test suite...",
  "tokens_in": 45210,
  "tokens_out": 3120,
  "cost": 0.42
}
```

The in-memory `SubagentProgress` uses camelCase (`toolCalls`, `lastTool`). Only
the file uses snake_case. Per-node files avoid write races between parallel
spawns and are cheap for the engine to read.

Durable stalled detection remains proposed: a heartbeat whose `updated_at` is
older than `max(5 s, 2 × poll interval)` becomes `stalled` in the projection
(§8.3). The live widget already marks a silent row `⚠ stalled`.

## 7. The live pipeline (landed)

The `SubagentProgress` reducer, the heartbeat writer, the
`attempt --begin`/`--end` calls, the live registry, and the render timer exist
today.

```text
runSubagent(w1) ─┐
runSubagent(w2) ─┼─▶ processLine reducer ─▶ SubagentProgress snapshot
runSubagent(w3) ─┘                          │
                                            ├─▶ onProgress            (live registry)
                                            ├─▶ HeartbeatWriter       (≤1 Hz)
                                            │     └─ .sliceme/<key>.progress_<node>.json
                                            └─▶ attempt --begin/--end (via runTracked)

landed:
  SubagentProgress ─▶ LiveProgress (one record per try)
                          ├─▶ LiveRenderer (one timer, ~4 Hz)
                          │     ├─ ctx.ui.setWidget("sliceme", lines)
                          │     └─ onUpdate (each spawn's own call)
                          └─▶ the same debounced heartbeat writer
```

`runSubagent` folds each event into one `SubagentProgress`:

- `turn_start` increments `turns`;
- `tool_execution_start` and `tool_execution_end` update `toolCalls`, the
  `tools` histogram, and `lastTool`/`lastToolArgs`;
- `message_update.usage` records the in-flight cumulative usage;
- `message_end` (assistant) captures `lastText` and commits that response's
  `tokensIn`/`tokensOut`/`cost`, then clears the in-flight usage so no double
  count occurs.

The live view cannot call the engine per frame, because `runSliceme` starts a
Python process. It renders from in-process state. The proposed `progress` action
is for durability, a second terminal, and the finished report. It reads the
heartbeat files plus SQLite, never the live registry.

The renderer keeps the view within pi's ten-line widget limit. It shows the
running subagents first. It then shows a window of four waves around the current
wave. In this way the first waves do not fill the view for the whole campaign.
The wave line derives its status from the live node statuses. It does not use the
cached `state.waves[].status`, which lags a replan.

## 8. The `progress` projection (proposed)

Not implemented. To add it, extend `sliceme/surface.py` and implement it in
`sliceme/service.py`. Sliceme generates the CLI from `surface.ACTIONS`.

```text
sliceme progress [--node ID]
```

The global `--json` flag selects the machine-readable form. The action joins the
DAG wave projection, `state.json`, the `attempts` table, the
`units`/`candidates`/`jobs` rows, and the heartbeat files. Abridged output:

```json
{
  "campaign": {},
  "now": 1733234500.0,
  "totals": {
    "nodes": 12, "done": 4, "running": 2, "pending": 6, "failed": 0,
    "waves": 4, "current_wave": 1, "elapsed": 3600.0,
    "worker_seconds": 4210.0, "turns": 40, "tool_calls": 210,
    "tokens_in": 123456, "tokens_out": 7890, "cost": 1.23,
    "attempts": 6, "jobs": 5,
    "verification_pass_rate": 0.8, "queue_wait_seconds": 42.0
  },
  "nodes": [
    {
      "id": "w1", "label": "human label", "wave": 0, "status": "running",
      "lead_time": 3600.0,
      "attempts": [
        {
          "attempt": 1, "unit": "w1", "status": "running",
          "started_at": 1733234401.0, "finished_at": null, "duration": 62.3,
          "last_tool": "bash", "last_tool_args": "cargo test",
          "turns": 7, "tool_calls": 23,
          "tokens_in": 45210, "tokens_out": 3120, "cost": 0.42,
          "last_activity_at": 1733234462.0, "heartbeat_age": 1.2, "stalled": false
        }
      ],
      "verification": {"status": "passed", "duration": 12.4}
    }
  ],
  "executor": {"counts": {}, "running_job": {"id": 7, "source": "node:w1"}}
}
```

### 8.1 Timing definitions (proposed)

- **Work-unit wall clock:** `attempt.started_at` to `finished_at`, or `now` for
  a running try.
- **Queue wait:** `job.started_at - job.requested_at`.
- **Verification time:** `jobs.duration`.
- **Node lead time:** the first try's start to the landing of the candidate.
- **Wave duration:** the first member try's start to the last member's landing.
- **Critical path:** the sum of the longest per-node lead time across waves.

### 8.2 Token and cost accounting (implemented in the reducer)

`message_update.usage` is cumulative for one assistant response. `runSubagent`
keeps the latest value for the in-flight response and commits it once at
`message_end` (assistant); it never adds every delta. It serializes the
committed totals into the heartbeat and passes them to `attempt --end` as
`--tokens-in`/`--tokens-out`/`--cost` when the run finishes.

### 8.3 Stalled detection (proposed)

Treat a heartbeat as stalled when `now - updated_at > max(5 s, 2 × poll interval)`.
The node still reports `running`, but the display shows `⚠ stalled` and the age
of the last event. Stalled is a display state. It does not change `state.json`
and does not fail a node by itself.

The live widget implements the display rule. The durable `stalled` field in the
proposed `progress` projection remains open.

## 9. Architectural suggestions

These suggestions follow impact order.

1. **Introduce one shared progress registry per coordinator process.** One
   registry (`liveAgents`) and one render timer now compose the widget, so
   parallel workers never overwrite each other. Implemented.

2. **Centralize campaign-state mutation.** One in-process
   `CampaignStateStore` per campaign branch now owns `.sliceme/<key>.state.json`.
   Every caller gets the same state object, so a parallel spawn completion
   cannot drop another one. `store.save` flushes it with an atomic write
   (temporary file plus rename). Implemented.

3. **Normalize the subagent event stream once.** Parse `pi --mode json` in one
   place into progress events, then reduce. The reducer exists; the separate
   event type does not.

4. **Separate the live sink from the durable sink behind one interface.** A
   `ProgressSink` with `event()` and `flush()` has an in-memory implementation
   for the widget and a file/`attempts` implementation for durability. Partly
   realized.

5. **Let the engine compute and the adapter format.** Define `progress` as the
   stable schema and implement `renderProgress` as a pure formatter over a
   subset of it. Not implemented.

6. **Keep the blocking spawn for v1.** Pi runs tool calls from one message in
   parallel, so several `spawnNode` calls already overlap. A background-spawn
   design adds lifecycle work and weakens the "a coordinator crash kills the
   workers" invariant.

7. **Prefer an atomic snapshot per node over a shared progress file.**
   Implemented.

8. **Do not add a watcher daemon.** A long-lived `sliceme progress --follow`
   process would violate the no-daemon rule (`reference.md` §6). A second
   terminal polls `sliceme progress` instead.

## 10. Phased delivery

**Phase 0 — live in-process display (P0). Done.** The normalized events and
the `onProgress` consumer, the `LiveProgress` registry, and the render timer
landed. `renderProgress` replaced `widget`, and `spawn` streams `onUpdate`.

**Phase 1 — durable heartbeats and tries (P1). Mostly done.** Landed: the
heartbeat files, the `attempts` table, the `attempt` action, the
`--begin`/`--end` wiring in `runTracked`, and the `SLICEME_ACTIONS` entry.
Remaining: the `progress` action and stalled detection.

**Phase 2 — campaign economics and the second terminal (P2). Not started.**
ETA, throughput, the tool histogram, wave and lead timings, verification pass
rate, cache hits, and the metric catalog in the report skeleton.

**Phase 3 — documentation.** `docs/reference.md` documents the `attempt` action
and the `attempts` table. The `progress` additions land with the Phase 1
remainder.

## 11. Metric catalog

Each metric below has a landed source. The `progress` projection that exposes
them together remains proposed.

| Group | Metric | Source |
|---|---|---|
| Wave | total waves, current wave, per-wave status and duration | `plan_dag_waves` + `state.json` + `attempts` |
| Work unit | status, wave, tries, wall clock, lead time | `state.json`, `attempts`, `units` |
| Executor | queued/running counts, queue wait, check duration, cache hits | `jobs` |
| Verification | pass count, pass rate, duration | `jobs` |
| Agent | turns, tool calls, tool histogram, tokens in/out, cost, last tool | `attempts`, heartbeats |
| Campaign | elapsed, total worker seconds, total tokens, total cost, ETA | rollup of the above |

## 12. Test plan

Landed:

- `tests/test_sessions.py` covers the `attempts` table, its additive creation
  on an older plane, and the `begin_attempt`/`end_attempt` metrics.
- `tests/test_cli.py` drives `sliceme attempt --begin`/`--end`.
- `tests/test_pi_package.py` asserts that `attempt` appears in
  `surface.ACTIONS` and `SLICEME_ACTIONS`, that `runSubagent` accepts a
  heartbeat, and that the heartbeat file uses the snake_case keys.
- `tests/test_pi_package.py::test_live_progress_view_is_wired` asserts the
  registry, the timer, the `onProgress` consumer, and the `onUpdate` stream.
- `tests/render_progress_test.mjs` drives `renderProgress` with fixed snapshots
  and widths: markers, durations, truncation, stalled rows, and the empty
  snapshot. `test_pi_package.py` runs it through the Node type stripper.
- `tests/state_store_test.mjs` drives `CampaignStateStore`: two callers share
  one state object and both mutations land, `save` writes atomically, and
  `save(false)` leaves the file alone. `test_pi_package.py` runs it the same
  way.

Proposed:

- `tests/test_progress.py` with known timestamps: durations, queue wait, wave
  rollups, totals, heartbeat merge, and stalled detection.
- Estimated-time-of-arrival suppression below two finished nodes.
- `tests/test_executor.py`: a timing assertion that the executor records
  `duration`.

## 13. Risks and decisions

- **Widget updates with a pending tool call.** The render timer calls
  `ctx.ui.setWidget` while a tool call waits. Each `spawn` also streams
  `onUpdate`, so the row still arrives if a timer update is lost.
- **Log volume and backpressure.** Debounce heartbeat writes (≤1 Hz plus a
  final flush) and keep consuming the JSON stream, because pi stalls when the
  pipe fills. Do not let rendering slow the line reader.
- **Lost updates.** The `CampaignStateStore` of §9 suggestion 2 now gives every
  caller one shared state object and flushes it atomically, so parallel spawn
  completions keep each other's node status. Two coordinator processes in one
  checkout remain out of scope.
- **Token accounting.** `message_update.usage` is cumulative per assistant
  response. Sum the latest value per `message_end`, not every delta.
- **Metric sensitivity.** Token and cost data live under the git-excluded
  `.sliceme/`, like the report and job tables.
- **Heartbeat casing.** The file uses snake_case and the in-memory snapshot
  uses camelCase. A new reader must use the file keys.
- **ETA honesty.** Agent work is not uniform. Show ETA as a rough marker and
  suppress it until samples exist.

## 14. Related work

`docs/sessions.md` depends on the durable half of this design. That half
exists: the `attempts` table and the per-node heartbeat files record how far the
current worker got. A resumed campaign uses them plus the preserved campaign
worktree to continue a paused node.

The remaining Priority 0 and Priority 2 items are independent of suspend and
resume. A `progress` action would give a second terminal the durable view.
