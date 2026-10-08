# Sliceme observability

Status: implemented. This document describes what the engine reports about a
campaign, and where child-run state lives.

## 1. Purpose

While a campaign runs, the coordinator and a second terminal answer these
questions from the engine:

1. How many waves and nodes exist, and which wave is current?
2. Which nodes are ready, running, done, or failed?
3. Is the campaign paused?
4. How many checks passed, failed, or stopped with an error?
5. What do the final report and the evidence document say?

The engine answers questions 1 to 4 through the `status` and `ready` verbs. The
report and the evidence document answer question 5. Child execution detail
belongs to pi-subagents.

## 2. The data

The engine keeps one durable store and two files.

- **`checks` table.** One terminal row per check run: the fingerprint, the
  source, the commit, the status, the duration, the exit code, and the output.
  The row serves `check --current`, the review evidence, and the delivery path.
- **`state.json`.** An optional, read-only legacy override that holds per-node
  status, the current wave, and the wave list. The engine never writes it; the
  engine reads it only when a legacy file is present. Git plus `state.db` are
  authoritative.
- **The report.** `.sliceme/<branch-key>.report.md`, written by
  `review --report`.
- **The evidence document.** `.sliceme/<branch-key>.evidence.json` (the complete
  evidence) and `.sliceme/<branch-key>.evidence.md` (the bounded Markdown that
  becomes the pull request body), written by `evidence`.

The engine keeps no per-child progress file, no `events.jsonl`, and no
heartbeat. pi-subagents owns the live child status, the events, and the control
surface.

### 2.1 Check counts

`status` returns a `checks` block from `Store.check_counts(campaign=key)`: the
number of terminal rows per status. The block is the cache health of one
campaign, not a queue. A `cached` result on a `check --current` reply means the
fingerprint already had a terminal row.

## 3. The status projections

### 3.1 The dense summary

The default human `sliceme status` output is the **dense summary**: a header,
one line per node in DAG order, and one line per wave. Its node and wave lines
come from `dag.json`, git, and the recorded candidates in `state.db`. The
engine uses `state.json` only as an optional, read-only legacy override. The
header's `target`/`worktree` come from the campaign config, so a second terminal
sees the same plan.

```text
campaign: dense
target:   feat/x  worktree: feat/x  base: main
design:   DESIGN.md
nodes:    2  wave size: 2
  w0 w1 [build] done — parser
  w1 w2 [-] running
wave 0 [done]: w1
wave 1 [pending]: w2
```

`--dense` asks for it explicitly; `--verbose` prints the full nested dump. With
`--json` the default is the nested dump, while `--dense --json` emits the
summary as JSON (the `lines` array holds the same text). A plane with no single
campaign falls back to a compact per-campaign plane list.

### 3.2 The nested dump

`status --verbose` or `status --json` returns the nested view:

- `dag_waves` (the scheduler's wave plan) and `dag_waves_error`;
- `ready` (the ready node ids) and `paused`;
- `checks` (the terminal-check counts);
- `units` and `candidates` with their `node`, `log`, `candidate`, and
  `verification` columns;
- `sandbox` (the resolved gate);
- `dag_merge` (the same-ownership contraction).

### 3.3 The loop read

`ready` returns the small object the workflow resource polls: `campaign`,
`ready`, `wave`, and `paused`. The engine computes `paused` from the
`<branch-key>.control.json` pause flag, so the sandbox needs no file access.

## 4. The simulation

`sliceme status --simulate` groups prepared candidates into DAG waves,
materializes each wave's combined tree, and runs the configured checks once over
the combined result. `--no-checks` plans only. The simulation is a dry run: it
changes no candidate, no branch, and no delivery base.

## 5. Where child state lives

The `sliceme.campaign` workflow resource launches each child through
pi-subagents. pi-subagents owns the child status, the event stream, the tool
scoping, and the run control. The engine receives only JSON from `runs.host`,
so it never reads a child's live state.

A wave with a stalled or failed child shows up as a node that never reaches
`recorded`. The coordinator reads `ready` again after the wave children settle,
and the next `record` attributes the changes that exist.

## 6. Constraints

- **No daemon.** Poll `sliceme status` or `ready`. Do not add `--follow`.
- **One synchronous runner.** The check runner writes one terminal row per run,
  so the count stays exact.
- **Rebuildable.** `state.json` is an optional, read-only legacy override that
  the engine never writes. Git and `state.db` win on conflict, so a stale
  `status` read self-corrects on the next call.
- **Private.** The check output, the report, and the evidence document stay
  under the git-excluded `.sliceme/`.

## 7. Tests

- `tests/test_status_summary.py` drives the dense `status` summary.
- `tests/test_checks.py` drives the check cache and `status` check counts.
- `tests/test_sessions.py` covers the `checks` columns and the migration.
- `tests/test_evidence.py` drives the evidence document.
- `tests/test_cli.py` drives the `status` modes.
- `tests/test_pi_package.py` checks the wiring and the resource grants.

## 8. Related work

- `docs/sessions.md` describes suspend and resume.
- `docs/database.md` documents the schema.
- `docs/reference.md` documents the `status`, `ready`, `check`, `review`, and
  `evidence` actions.
