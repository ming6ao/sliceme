# Sliceme observability

Status: implemented. This document describes the live view and the durable time
and tool metrics for one campaign.

## 1. Purpose

While a campaign runs, the coordinator and a second terminal answer these
questions:

1. How many waves and nodes exist, and which wave is current?
2. Which work units run, wait, finish, or fail?
3. What does each subagent do now, and for how long?
4. Where does the time go, by agent role, by tool, and by command?
5. Which tools and which commands take the longest?
6. What is the verification cost?

The live view is a pi widget. The durable view is the `progress` action.

## 2. The data

Two durable stores and one live signal feed the view.

- **`attempts` table.** One row per subagent run. It holds the wall clock, the
  tool seconds, the tool and command durations, the turns, the tool calls, the
  tokens, the cost, and the exit code.
- **Heartbeat file.** `runSubagent` writes
  `.sliceme/<branch-key>.progress_<node>.json` at most once per second while a
  subagent runs. It holds the same counters plus the last tool and the last
  text.
- **`jobs` table.** The executor check time and the queue wait.
- **Live reducer.** `runSubagent` folds the `pi --mode json` stream into one
  `SubagentProgress` snapshot and emits it for every event.

The heartbeat file uses snake_case. The in-memory snapshot uses camelCase.

### 2.1 Time split

- **Wall clock:** `finished_at - started_at`.
- **Tool time:** the sum of every tool call duration.
- **Thinking time:** the wall clock minus the tool time. It includes the
  provider connection and the response streaming.

`runSubagent` pairs `tool_execution_start` and `tool_execution_end` by
`toolCallId`. Pi can run tool calls from one message at the same time, so the
id is the only correct key.

### 2.2 Commands

A command groups by program name only: the first token, after a leading
`cd <dir> &&` and any `NAME=value` assignments. For example,
`tools/nanochat build //src:tokenizer` groups as `tools/nanochat`.

The reducer caps the command map at 200 keys per run. The least costly keys
merge into `(other)`.

### 2.3 Verifier and executor accounting

A verifier tool call is a real tool call. It folds into the tool rollup and is
tagged with the `agent` role. The executor check time stays in the
`verification` block, because the executor is not a pi subagent.

## 3. The live view

One registry and one render timer per coordinator process compose the widget.
`renderProgress` is pure. The timer runs at 4 hertz, so elapsed time advances
between events.

Pi limits the widget to ten lines. It shows:

- a header;
- an aggregate line (wave, done, running, failed);
- a metrics line: elapsed, tools percent, thinking percent, slowest tool;
- one row per running subagent: elapsed, turns, tools, the tool and thinking
  split, and the active tool with its age;
- a window of waves around the current wave.

A silent running row shows `⚠ stalled` after five seconds. The `spawn` tool
also streams its own row through `onUpdate`.

## 4. The durable view

```bash
sliceme progress [--node ID] [--campaign REF]
```

The action joins the DAG waves, `state.json`, `attempts`, `jobs`, and the
heartbeat files. It prints one stable JSON document, or a human summary. The
`--node` flag narrows the view to one node.

Abridged output:

```json
{
  "totals": {
    "nodes": 8, "done": 4, "running": 1, "pending": 3,
    "elapsed": 6531.2, "wall_seconds": 5162.0,
    "tool_seconds": 700.0, "thinking_seconds": 4462.0,
    "turns": 120, "tool_calls": 340,
    "tokens_in": 800000, "tokens_out": 200000, "cost": 3.1,
    "queue_wait_seconds": 1.6
  },
  "by_agent": {
    "worker":   {"tool_seconds": 600.0, "thinking_seconds": 4000.0},
    "verifier": {"tool_seconds": 100.0, "thinking_seconds": 462.0}
  },
  "tools": [
    {"tool": "bash", "seconds": 540.0, "calls": 200, "avg": 2.7,
     "by_agent": {"worker": 480.0, "verifier": 60.0}}
  ],
  "commands": [
    {"command": "tools/nanochat", "tool": "bash", "seconds": 190.0, "calls": 3}
  ],
  "verification": {"executor_seconds": 220.0, "pass_rate": 0.8},
  "nodes": [
    {"id": "w1", "wave": 0, "status": "running", "tool_seconds": 260.0,
     "thinking_seconds": 210.0, "turns": 34, "tool_calls": 44,
     "heartbeat_age": 1.2, "stalled": false}
  ],
  "executor": {"queued": 0, "running": 1, "passed": 4}
}
```

## 5. Stalled detection

Sliceme marks a running node stalled when its heartbeat is older than five
seconds. A finished node is never stalled. Stalled is a display state. It does
not change `state.json` and does not fail a node.

## 6. Constraints

- **No daemon.** Poll `sliceme progress`. Do not add `--follow`.
- **Bounded cost.** Debounce the heartbeat writes, use one render timer, and
  bound the command map.
- **Approximate cost.** The token and cost figures are rollups of the provider
  usage.
- **Private metrics.** The token and cost data stay under the git-excluded
  `.sliceme/`.

## 7. Tests

- `tests/render_progress_test.mjs` drives the renderer with fixed snapshots.
- `tests/metrics_test.mjs` drives the reducer helpers with a fixed event list.
- `tests/test_progress.py` drives the `progress` projection.
- `tests/test_sessions.py` covers the `attempts` columns and the migration.
- `tests/test_cli.py` drives `attempt --end` and `progress`.
- `tests/test_pi_package.py` checks the wiring and the action lockstep.

## 8. Related work

- `docs/sessions.md` uses the `attempts` table for suspend and resume.
- `docs/database.md` documents the schema.
- `docs/reference.md` documents the `attempt` and `progress` actions.
