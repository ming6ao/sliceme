# Sliceme observability design

Status: partially implemented. The durable half of Priority 1 landed with
`docs/sessions.md`:

- the `attempts` table and the `attempt --begin`/`--end` engine action
  (`sliceme/store.py`, `sliceme/surface.py`, `sliceme/service.py`);
- debounced per-node heartbeat files and the stream reducer in
  `integrations/pi/common.ts::runSubagent`;
- the `runTracked` wrapper that records an attempt and a heartbeat for the
  planner, each worker, and each verifier
  (`integrations/pi/coordinator.ts`).

Still proposed: the live in-process display (Priority 0), the `progress`
projection and stalled detection (Priority 1), and the campaign-economics
polish (Priority 2).

This document describes how to make a running campaign observable, with the
primary goal of a **live, tqdm/rich-style progress display for the parallel
working subagents** (the workers a wave spawns). A second goal is a durable,
queryable projection so a second terminal or a finished report can show the
same numbers.

It is grounded in the current engine and adapter:

- `sliceme/ownership.py` already projects the DAG into waves
  (`plan_dag_waves`, `DagWave`).
- `sliceme/store.py` already persists `units`, `candidates`,
  `fingerprints`, `verifications`, and `jobs`, including `jobs.duration`, plus
  the landed `attempts` and `campaign_sessions` tables.
- `sliceme/executor.py` already exposes the single runner's queue
  (`Executor.status`, `Store.job_counts`).
- `integrations/pi/common.ts::runSubagent` already streams each subagent's
  full `pi --mode json` output to a per-node log, reduces it into a
  `SubagentProgress` snapshot, exposes an `onProgress` callback, and writes a
  debounced heartbeat file.
- `integrations/pi/coordinator.ts::widget` already renders wave lines, and
  multiple tool calls from one assistant message can run in parallel, so
  several `spawn` calls execute concurrently and block on their own
  `runSubagent`.

## 1. Goals and non-goals

### Goals

While a campaign runs, answer these questions live, from the coordinator
session and from a second terminal:

1. How many waves and nodes exist, how many are done, and which wave is current?
2. Which work units are running, pending, done, or failed?
3. For each running subagent: what is it doing right now, and for how long?
4. What are the aggregate and per-agent costs (turns, tool calls, tokens, cost)?
5. What are the timings (wall clock, queue wait, verification, lead time, ETA)?

### Non-goals

- A long-lived daemon or a second terminal renderer (reference.md §6). The
  live view is an in-process renderer plus small on-disk snapshots.
- Multiple concurrent campaigns per plane.
- Precise scientific billing. Token and cost figures are approximate rollups of
  the provider's cumulative usage.
- Replacing the pi TUI. The display is a `ctx.ui.setWidget` region and the
  per-tool `onUpdate` stream, nothing more.

## 2. Most important features first

The design is deliberately staged so a useful display exists before any new
database table or engine action. "Status" is against the current tree; "Where"
names the layer that owns the feature.

**Priority 0 — the live multi-subagent view (no persistence required).**

| # | Feature | Status | Where |
|---|---|---|---|
| P0.1 | Normalized progress events reduced from the `pi --mode json` stream | Partial: `runSubagent` reduces into a `SubagentProgress` snapshot; the separate event vocabulary in §6.1 is not built | `common.ts::runSubagent` |
| P0.2 | One in-process progress registry for all running subagents | Proposed | coordinator extension |
| P0.3 | One render timer (~4–10 Hz) that composes the widget | Proposed | coordinator extension |
| P0.4 | Per-subagent row: state, node, current tool + argument, elapsed | Proposed | coordinator extension |
| P0.5 | Campaign aggregate line: done/total, wave k/n, elapsed, totals | Proposed | coordinator extension |
| P0.6 | `spawn` streams its own row through `onUpdate` | Proposed (`onUpdate` is currently ignored) | coordinator extension |
| P0.7 | Width-safe, theme-aware string renderer | Proposed | coordinator extension |

**Priority 1 — durable and queryable (crash-safe, second terminal).**

| # | Feature | Status | Where |
|---|---|---|---|
| P1.1 | Debounced per-node heartbeat snapshot file | Implemented | `common.ts` + `.sliceme/` |
| P1.2 | `attempts` table and `attempt --begin/--end` | Implemented | `store.py`, `surface.py`, `service.py` |
| P1.3 | `progress` action (`sliceme progress`) | Proposed | `surface.py`, `service.py` |
| P1.4 | Stalled detection | Proposed | `service.py` |
| P1.5 | New actions added to `SLICEME_ACTIONS` | `attempt` landed; `progress` proposed | `unit.ts` |

**Priority 2 — polish and campaign economics.**

| # | Feature | Status | Where |
|---|---|---|---|
| P2.1 | ETA and throughput rates (tokens/min, tools/min) | Proposed | `service.py`, renderer |
| P2.2 | Wave durations, node lead time, critical-path estimate | Proposed | `service.py` |
| P2.3 | Tool histogram, verification pass rate, cache hits | Proposed | `progress`, renderer |
| P2.4 | Executor/verification section in the widget | Proposed | coordinator extension |
| P2.5 | Metrics in the report skeleton | Proposed | `campaign.py` |

## 3. What already exists

- **Wave projection is deterministic and complete.** `plan_dag_waves` produces
  `DagWave(index, members, conflicts)`, surfaced by `status` as `dag_waves` and
  reconciled into `state.waves` by `reconcileWaves`.
- **Some timing is already persisted.** `units.created_at`/`updated_at`,
  `candidates.created_at`/`updated_at`, `verifications.duration`, and the
  `jobs` table (`requested_at`, `started_at`, `finished_at`, `duration`,
  `exit_code`).
- **Attempt fidelity is persisted.** `attempts` records one planner, worker, or
  verifier run (`started_at`, `finished_at`, `duration`, `exit_code`, `turns`,
  `tool_calls`, `tools`, `tokens_in`, `tokens_out`, `cost`, `last_tool`,
  `last_activity_at`, `error`), written through
  `sliceme attempt --begin`/`--end`.
- **A live activity signal is written.** While a subagent runs, `runSubagent`
  reduces `turn_start`, `tool_execution_start`/`tool_execution_end`,
  `message_update.usage`, and `message_end` into a `SubagentProgress` snapshot
  and flushes it, at most once per second, to
  `.sliceme/<branch-key>.progress_<node>.json`.
- **Executor queue state** is available from `Store.job_counts()` and
  `Executor.status()`.
- **Per-worker logs** contain the full `pi --mode json` event stream, including
  `tool_execution_start`/`tool_execution_end`, `turn_start`/`turn_end`,
  `message_end`/`message_update`, and cumulative `usage`.
- **Parallel tool calls are supported.** Pi can run several tool calls from one
  assistant message concurrently, so a wave's `spawn` calls already overlap.

## 4. Gaps

1. **No live updates.** `coordinator.ts::spawnNode` awaits `runSubagent`, and
   the widget is rendered only when `status` or `start` runs, so it is stale
   while any worker runs. The heartbeat is written but nothing displays it live.
2. **No live activity signal.** The heartbeat records the current tool call and
   the last assistant text, but `onProgress` has no consumer and the widget
   does not read the heartbeat.
3. **No combined projection.** Nothing merges work-unit status, wave status,
   timings, executor state, agent metrics, and heartbeats into one snapshot.
   The raw pieces exist in SQLite and in `.sliceme/*.progress_<node>.json`; the
   `progress` action that would join them is not built.
4. **`state.json` has no timestamps** and is read-modify-written by each spawn,
   so parallel spawn completions can lose updates (see §11.2).
5. **No stalled detection.** A dead or pipe-blocked worker's heartbeat simply
   stops advancing; nothing labels it.

## 5. Design principles

- **The engine owns durable state; the adapter owns the live view.** Durable
  numbers come from SQLite and heartbeat files through the `progress` action.
  The animated view is presentation state derived from the same reducer.
- **One renderer, one registry.** Exactly one component in the coordinator
  process composes the widget. Subagents publish events; they never write the
  widget.
- **One event vocabulary.** Reduce the `pi` stream once into a small
  `ProgressEvent` set, and share that reducer between the live view and the
  durable writer. Do not parse the raw stream twice.
- **The renderer is pure.** `renderProgress(snapshot, options) -> string[]` has
  no I/O, so it is unit-testable and reusable for `onUpdate` and the second
  terminal.
- **Everything is reconstructable** from `.sliceme/` plus git after a crash.
- **Bound the cost.** Heartbeats are debounced, the render timer is single, and
  no per-event file write or per-frame subprocess is allowed.

## 6. Architecture for live multi-subagent progress

### 6.1 Metric reduction in `runSubagent` (implemented)

`runSubagent` iterates the JSON lines in `processLine` and folds each event
into one `SubagentProgress` object (camelCase in memory):

- `turn_start` increments `turns`.
- `tool_execution_start` / `tool_execution_end` update `toolCalls`, the `tools`
  histogram, and `lastTool` / `lastToolArgs`.
- `message_update.usage` records the in-flight response's cumulative usage.
- `message_end` (assistant) captures the authoritative `lastText` and commits
  that response's `tokensIn` / `tokensOut` / `cost` (see §8.2), then clears the
  in-flight usage so it is not counted twice.

The snapshot is passed to the optional `onProgress` callback and serialized to
the heartbeat file. There is **no separate `ProgressEvent` type yet**; the
reducer writes the snapshot directly. The transport-neutral vocabulary below is
still the intended refinement for the live renderer, which needs per-event
timestamps to compute rates without re-parsing the log.

```jsonc
{"t":"attempt_started","node":"w1","attempt":1,"unit":"w1","agent":"worker","at":1733234401.0}
{"t":"turn","node":"w1","attempt":1,"turn":7,"at":1733234410.0}
{"t":"tool_started","node":"w1","attempt":1,"tool":"bash","args":"cargo test","at":1733234411.0}
{"t":"tool_finished","node":"w1","attempt":1,"tool":"bash","ok":true,"ms":812,"at":1733234411.8}
{"t":"usage","node":"w1","attempt":1,"input":45210,"output":3120,"cost":0.42,"at":1733234412.0}
{"t":"text","node":"w1","attempt":1,"text":"Running the CPU test suite...","at":1733234412.0}
{"t":"attempt_finished","node":"w1","attempt":1,"status":"ok","exit":0,"at":1733234463.2}
```

Proposed mapping, once the event type exists:

- `agent_start` → `attempt_started`.
- `turn_start` / `turn_end` → `turn` (count).
- `tool_execution_start` → `tool_started` (`toolName`, a short argument summary).
- `tool_execution_end` → `tool_finished` (success plus duration).
- `message_update.usage` → `usage` (cumulative per response; see §8.2).
- `message_end` (assistant) → `text` (authoritative last assistant text).
- `agent_settled` → `attempt_finished`.

### 6.2 Registry, renderer, and the durable sink

Of the pipeline below, only the `SubagentProgress` reducer, the debounced
heartbeat writer, and the `attempt --begin`/`--end` calls are built. The
in-process registry and render timer are proposed.

```text
runSubagent(w1) ─┐
runSubagent(w2) ─┼─▶ processLine reducer ─▶ SubagentProgress snapshot
runSubagent(w3) ─┘                          │
                                            ├─▶ onProgress            (implemented; no consumer yet)
                                            │
                                            ├─▶ HeartbeatWriter       (implemented, ≤1 Hz)
                                            │     └─ .sliceme/<key>.progress_<node>.json
                                            │
                                            └─▶ attempt --begin/--end (implemented, via runTracked)

proposed:
  SubagentProgress ─▶ LiveProgress (one record per attempt)
                          ├─▶ LiveRenderer (one timer, ~4–10 Hz)
                          │     ├─ ctx.ui.setWidget("sliceme", lines)
                          │     └─ onUpdate (each spawn's own call)
                          └─▶ the same debounced heartbeat writer
```

Because a render must be cheap and `runSliceme` spawns a Python process, the
live view cannot call the engine per frame. It would render from in-process
state. The engine `progress` action (§6.5) is for durability, a second
terminal, and the finished report; it reads the heartbeat files plus SQLite,
never the live registry.

### 6.3 Durable heartbeat files (implemented)

Per running node, `runSubagent` writes
`.sliceme/<branch-key>.progress_<node>.json` atomically (`writeJson`), at most
once per second plus one final flush. It uses the `.sliceme/` snake_case
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

The in-memory `SubagentProgress` that feeds `onProgress` is camelCase
(`toolCalls`, `lastTool`, ...); only the file is snake_case. Per-node files
avoid write races between parallel spawns and are cheap for the engine to read.

Proposed: a heartbeat whose `updated_at` is older than
`max(5 s, 2 × poll interval)` is reported as `stalled` (§8.3). That detection is
not implemented.

### 6.4 Durable `attempts` table (implemented)

`attempts` is created by the schema script in `sliceme/store.py`
(`CREATE TABLE IF NOT EXISTS`), so an existing plane gains it on the next
`Store` open; this is the same additive approach as the column migrations in
`Store._migrate` (`jobs.timeout`, `candidates.node`).

```sql
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node TEXT NOT NULL,
  unit TEXT,
  attempt INTEGER NOT NULL DEFAULT 1,
  agent TEXT NOT NULL DEFAULT 'worker',
  status TEXT NOT NULL DEFAULT 'running',
  started_at REAL NOT NULL,
  finished_at REAL,
  duration REAL,
  exit_code INTEGER,
  turns INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  tools TEXT,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  last_tool TEXT,
  last_activity_at REAL,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_attempts_node ON attempts(node);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON attempts(status);
```

An attempt is one subagent run for one node: a planner, a worker, or a verifier.
`coordinator.ts::runTracked` calls `attempt --begin` before `runSubagent` and
`attempt --end` after, for all three. `--end` finishes the latest running
attempt for the node (optionally a specific `--attempt`).

```text
sliceme attempt --begin --node w1 --attempt 1 [--unit w1] [--agent worker]
sliceme attempt --end   --node w1 --attempt 1 --status ok --exit-code 0 \
                        [--turns 7] [--tool-calls 23] [--tokens-in 45210] \
                        [--tokens-out 3120] [--cost 0.42] [--tools '{"bash":6}']
```

The surface parity test requires every action to appear in
`integrations/pi/unit.ts::SLICEME_ACTIONS`; `attempt` (and, from
`docs/sessions.md`, `resume` and `sessions`) are already listed.

### 6.5 The `progress` projection (proposed)

Not implemented. To add it, extend `sliceme/surface.py` (the source of truth)
and implement in `sliceme/service.py`; the CLI is generated from
`surface.ACTIONS`:

```text
sliceme progress [--node ID]
```

The CLI's global `--json` flag selects the machine-readable form. It would join
the DAG wave projection, `state.json`, the `attempts` table, existing
`units`/`candidates`/`verifications`/`jobs` rows, and the heartbeat files.
Output shape (abridged):

```json
{
  "campaign": {},
  "now": 1733234500.0,
  "totals": {
    "nodes": 12, "done": 4, "running": 2, "pending": 6, "failed": 0,
    "waves": 4, "current_wave": 1, "elapsed": 3600.0,
    "worker_seconds": 4210.0, "turns": 40, "tool_calls": 210,
    "tokens_in": 123456, "tokens_out": 7890, "cost": 1.23,
    "attempts": 6, "verifications": 5,
    "verification_pass_rate": 0.8, "queue_wait_seconds": 42.0
  },
  "waves": [
    {
      "index": 0, "status": "done", "members": ["w1", "w2"],
      "started_at": 1733234000.0, "finished_at": 1733234400.0,
      "duration": 400.0, "integrated": ["w1", "w2"]
    }
  ],
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
      "candidate": {}, "verification": {"status": "passed", "duration": 12.4},
      "job": {}
    }
  ],
  "executor": {
    "counts": {},
    "running_job": {
      "id": 7, "source": "node:w1", "wave": 0,
      "command": "cargo test", "elapsed": 88.0
    }
  }
}
```

## 7. Rendering rules (tqdm/rich-style)

Proposed. `renderProgress` is a pure function of a snapshot and a frame
counter. Plain text lines are used because the widget accepts a string array,
including in remote-procedure-call mode. Styling uses the active pi theme.

```text
sliceme  ⣾ campaign  4/12 nodes · wave 1/4 · 01:00:00 · ETA ~00:22:30 · 131k tok · $1.23
  wave 0  ✓ done                    02:01
  wave 1  ● w1 edit src/api/foo.rs  01:02   7 turns  23 tools  45k tok
          ● w2 bash cargo test      00:48   4 turns  11 tools   8k tok
          · w3 queued                --:--
  wave 2  · w4 queued                --:--
executor  ⣽ node:w1 checks            00:01:30   queued 0
```

Rules:

- **Markers:** `✓` done, `●` running, `✗` failed, `⚠` stalled, `·` pending,
  `⣾⣽⣻⢿⡿⣟⣯⣷` spinner frames for running rows.
- **Aggregate bar:** a fixed-width progress bar plus `done/total`, current wave,
  elapsed, an approximate ETA, tokens, and cost. The bar is optional; the
  counters are mandatory.
- **Per-subagent row:** node, current tool with a shortened argument, then
  elapsed, turns, tools, tokens. Show `last_text` only when it fits, or when the
  node is stalled.
- **Verification/executor row:** one line for the single runner: current job
  source, elapsed, and queued count.
- **Elapsed clock:** `mm:ss` under an hour, `hh:mm:ss` above.
- **Colors:** running = accent, done = success, failed = error, stalled =
  warning, pending = muted.
- **Width safety:** truncate with `truncateToWidth` and measure with
  `visibleWidth`; never assume a fixed terminal width or the number of columns
  a Unicode marker occupies.
- **ETA:** show `~` and only when at least two nodes have finished. Estimate
  `average finished attempt seconds × remaining nodes ÷ concurrency`,
  clamped to a sane minimum. Never show a bare number as if precise.
- **Throughput:** tokens per minute and tools per minute, from a short window
  when event history exists, otherwise cumulative divided by elapsed.
- **Refresh:** one timer at 4–10 Hz while any attempt is running; stop the timer
  when the registry is empty, and render once on the final frame so the last
  state is not lost.

Today `widget` renders a static per-wave line from `state.waves`; replacing it
with `renderProgress` is part of Priority 0.

## 8. Data model details

### 8.1 Timing definitions (proposed projection)

- **Work-unit wall clock:** `attempt.started_at` to `finished_at`, or `now` for
  a running attempt.
- **Queue wait:** `job.started_at - job.requested_at`.
- **Verification time:** `verifications.duration`.
- **Node lead time:** the first attempt's start to when the candidate lands
  (`candidates.updated_at` when the status becomes `landed`).
- **Wave duration:** the first member attempt's start to the last member's
  landing.
- **Critical path:** the sum of the longest per-node lead time across waves, a
  simple deterministic estimate from the same data.

### 8.2 Token and cost accounting (implemented in the reducer)

`message_update.usage` is cumulative for one assistant response. `runSubagent`
keeps the latest value for the in-flight response and commits it once at
`message_end` (assistant); it never adds every delta. The committed totals are
serialized into the heartbeat and passed to `attempt --end` as
`--tokens-in`/`--tokens-out`/`--cost` when the run finishes.

### 8.3 Stalled detection (proposed)

A heartbeat is stalled when `now - updated_at > max(5 s, 2 × poll interval)`.
The node still reports `running`, but the display shows `⚠ stalled` and the age
of the last event. Stalled is a display state; it does not change `state.json`
and does not fail a node by itself.

### 8.4 What each subagent is doing (proposed resolution)

1. An executor job is running for that node or wave: show the check command and
   elapsed time.
2. A fresh heartbeat exists: show `last_tool` with a short argument, the last
   assistant text, and the age of the last event.
3. No heartbeat and the node reports `running`: show `stalled` with the age.
4. Otherwise: show the status and the last verdict.

Step 2 already has its data source (the heartbeat file); no renderer consumes it
yet.

## 9. Metric catalog

Every metric below has a landed source; the `progress` projection that would
expose them together is proposed.

| Group | Metric | Source |
|---|---|---|
| Wave | total waves, current wave, per-wave status and duration | `plan_dag_waves` + `state.json` + `attempts` |
| Work unit | status, wave, attempts, wall clock, lead time | `state.json`, `attempts`, `units` |
| Executor | queued/running counts, queue wait, check duration, cache hits | `jobs`, `verifications` |
| Verification | pass count, pass rate, duration | `verifications` |
| Agent | turns, tool calls, tool histogram, tokens in/out, cost, last tool | `attempts`, heartbeats |
| Campaign | elapsed, total worker seconds, total tokens, total cost, ETA | rollup of the above |

## 10. Interface changes

### Landed

- `store.py`: the `attempts` table, `create_attempt`, `finish_attempt`,
  `get_attempt`, `list_attempts`, `latest_attempt`, `find_running_attempt`.
- `service.py`: `begin_attempt`, `end_attempt`, `attempts`.
- `surface.py`: the `attempt` action. `cli.py` is generated from
  `surface.ACTIONS` and needed no manual change.
- `unit.ts`: `attempt` added to `SLICEME_ACTIONS`.
- `common.ts`: `runSubagent` reduces the stream into `SubagentProgress`,
  accepts `onProgress` and a heartbeat path, and debounces heartbeat writes to
  at most one per second.
- `coordinator.ts`: `runTracked` calls `attempt --begin`/`--end` and passes the
  heartbeat path for the planner, workers, and verifiers.

### Remaining

- `service.py`: `progress()` (joining waves, `state.json`, SQLite rows, executor
  queue, and heartbeat files).
- `surface.py`: the `progress` action.
- `unit.ts`: `progress` added to `SLICEME_ACTIONS`.
- `coordinator.ts`:
  - add the single `LiveProgress` registry and the render timer;
  - make `widget` render `renderProgress` instead of the current per-wave lines;
  - stream each spawn's row through the tool `onUpdate` callback;
  - include wave count, current wave, per-node elapsed, and totals in
    `summarise`;
  - add `progress` to the coordinator action list so the model can request a
    snapshot between spawns.

## 11. Architectural changes that make observability easy

These are suggestions, ordered by impact. P0 items do not depend on them; they
remove the sharp edges a live display would otherwise hit.

1. **Introduce one shared progress registry per coordinator process.** Today
   each `spawnNode` closure owns a `state` object and each renders the widget on
   demand. A single registry with one render timer is the difference between a
   smooth display and three workers overwriting each other. This is the single
   most important change. Not implemented.

2. **Centralize campaign-state mutation.** `spawnNode` reads
   `.sliceme/<key>.state.json`, mutates one node, and writes the whole file back
   when `runSubagent` resolves. Parallel spawns therefore race: the second
   writer can drop the first node's new status. Replace the read-modify-write
   with one in-process `CampaignStateStore` guarded by an async mutex, flushing
   atomically. This also gives the renderer a consistent snapshot. Not
   implemented.

3. **Normalize the subagent event stream once.** Parse `pi --mode json` in one
   place into `ProgressEvent`s, then reduce. Both the live view and the durable
   writer consume the reducer instead of re-parsing raw events. This avoids two
   subtly different notions of "turns" or "tokens". The reducer exists; the
   event type does not.

4. **Separate the live sink from the durable sink behind one interface.** A
   `ProgressSink` with `event()` and `flush()` has an in-memory implementation
   for the widget and a file/`attempts` implementation for durability. The
   adapter never computes durable metrics; the engine `progress` action is the
   only durable formatter. Partially realized: the heartbeat and `attempt` sinks
   exist, but there is no shared `ProgressSink` abstraction.

5. **Make the engine compute, the adapter only format.** Define `progress` as
   the stable schema and implement `renderProgress` as a pure formatter over a
   subset of it. Then the widget, `onUpdate`, the second terminal, and the
   report can share one renderer, and the renderer is unit-testable without a
   terminal. Not implemented.

6. **Keep spawn blocking for v1; revisit only if it hurts.** Pi runs tool calls
   from one message in parallel, so several `spawnNode` calls already overlap
   and a timer can animate during them. A background-spawn design (return a
   handle immediately, collect later) would decouple progress from the tool call
   but adds lifecycle work and weakens the "a coordinator crash kills the
   workers" invariant. Treat it as a decision gate, not a prerequisite.

7. **Prefer an atomic snapshot per node over a shared progress file.**
   Implemented: parallel writers never append to one file; each node has its own
   `progress_<node>.json` written atomically. An append-only per-node event log
   for rate/ETA history remains optional.

8. **Do not add a watcher daemon.** A long-lived `sliceme progress --follow`
   process would simplify a second terminal but violates the no-daemon rule
   (reference.md §6). A second terminal polls `sliceme progress` instead.

## 12. Phased delivery

**Phase 0 — live in-process display (P0). Not started.**
Add the normalized reducer events and a consumer for `onProgress`; add the
single `LiveProgress` registry and render timer to `coordinator.ts`; replace
`widget` with `renderProgress`; stream `onUpdate` from `spawn`. This delivers
the tqdm/rich-style multi-subagent view with no schema or database change.

**Phase 1 — durable heartbeats and attempts (P1). Mostly done.**
Landed: the debounced heartbeat files, the `attempts` table, the `attempt`
action, `--begin`/`--end` wiring in `runTracked`, and the `SLICEME_ACTIONS`
entry. Remaining: the `progress` action and stalled detection.

**Phase 2 — campaign economics and the second terminal (P2). Not started.**
ETA, throughput, the tool histogram, wave/lead timings, verification pass rate,
and cache hits; the executor section in the widget; the metric catalog in the
report skeleton.

**Phase 3 — documentation.**
`docs/reference.md` already documents the `attempt` action and the `attempts`
table; the `progress` additions land with Phase 1's remainder.

## 13. Test plan

Landed:

- `tests/test_sessions.py` covers the `attempts` table and its additive
  creation on an older plane (`MigrationTests`), plus `begin_attempt` /
  `end_attempt` metrics (`AttemptTests`).
- `tests/test_cli.py` drives `sliceme attempt --begin`/`--end` end to end.
- `tests/test_pi_package.py` asserts `attempt` appears in `surface.ACTIONS` and
  in `SLICEME_ACTIONS`, that `runSubagent` accepts a heartbeat and the
  coordinator passes `heartbeatPath`, and that the heartbeat file uses the
  documented snake_case keys (`tool_calls`, `last_tool`, `last_text`).

Proposed:

- `tests/test_progress.py`: build a plane, create units, candidates, jobs,
  attempts, and heartbeat files with known timestamps; assert durations, queue
  wait, wave rollups, totals, heartbeat merge, and stalled detection.
- A unit test for `renderProgress` with fixed snapshots and widths: markers,
  truncation of a long tool argument, ETA suppression below two finished nodes,
  and stalled rows.
- A structural test that `runSubagent` still preserves the final assistant text
  after the reducer change.
- `tests/test_executor.py`: unchanged behavior, plus a timing assertion that
  `duration` is recorded.

## 14. Risks and decisions

- **Widget updates during a pending tool call.** The render timer and the tool
  `onUpdate` callback are the only ways to observe a worker from the same
  session while `await runSubagent` is pending. Confirm with the installed pi
  version that a timer may call `ctx.ui.setWidget` while a tool call is
  outstanding; if not, `onUpdate` still covers the same session.
- **Log volume and backpressure.** Heartbeat writes must be debounced (they
  are, to ≤1 Hz plus a final flush), and the JSON stream must keep being
  consumed (pi stalls when the pipe fills), which the current `processLine`
  loop already does. Do not let rendering slow the line reader.
- **Lost updates.** Without §11.2, parallel spawn completions can clobber
  `state.json`. Fix this before enabling many parallel spawns, not after.
- **Token accounting semantics.** `message_update.usage` is cumulative per
  assistant response, so sum the latest value per `message_end`, not every
  delta. The reducer follows this.
- **Metric sensitivity.** Token and cost data are written under the
  git-excluded `.sliceme/`, matching the existing report and job tables.
- **Heartbeat casing.** The on-disk heartbeat uses the `.sliceme/` snake_case
  convention; the in-memory `SubagentProgress` is camelCase. Any new reader must
  use the file's keys (`tool_calls`, `last_tool`, `last_text`), not the
  in-memory ones.
- **ETA honesty.** Agent work is not uniform; show ETA as a rough marker and
  suppress it until there are samples.

## 15. Related work

`docs/sessions.md` depends on the durable half of this design, and that half is
implemented: the `attempts` table and per-node heartbeat files record how far
the current worker got, and a resumed campaign uses them (plus the preserved
campaign worktree) to continue a paused node instead of restarting it.

The remaining Priority 0 and Priority 2 items here are independent of session
suspend/resume; the `progress` action would give a second terminal the durable
view that `docs/sessions.md`'s `/campaigns` surface does not provide.
