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

- **`verdicts` table.** One terminal row per check run: the fingerprint, the
  source, the commit, the status, the duration, the exit code, and the output.
  The row serves `check`, the review evidence, and the delivery path.
- **The session descriptor.** `.sliceme/<branch-key>.session.json` holds the
  adapter's session binding and the per-node status map at suspend time. There
  is no `state.json`; git plus `state.db` are authoritative.
- **The report.** `.sliceme/<branch-key>.report.md`, written by
  `report`.
- **The evidence document.** `.sliceme/<branch-key>.evidence.json` (the complete
  evidence) and `.sliceme/<branch-key>.evidence.md` (the bounded Markdown that
  becomes the pull request body), written by `evidence`.

The engine keeps no per-child progress file, no `events.jsonl`, and no
heartbeat. pi-subagents owns the live child status, the events, and the control
surface.

### 2.1 Check counts

`status` returns a `checks` block from `Store.verdict_counts(campaign=key)`: the
number of terminal rows per status. The block is the cache health of one
campaign, not a queue. A `cached` result on a `check` reply means the
fingerprint already had a terminal row.

## 3. The status projections

### 3.1 The dense summary

The default human `sliceme status` output is the **dense summary**: a header,
one line per node in DAG order, and one line per wave. Its node and wave lines
come from `dag.json`, git, the recorded records in `state.db`, and the
session descriptor. The
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
- the campaign writer and `records` with their `node`, `log`, `record`,
  and `verification` columns;
- `sandbox` (the resolved gate);
- `dag_merge` (the same-ownership contraction).

### 3.3 The loop read

`ready` returns the small object the workflow resource polls: `campaign`,
`ready`, `wave`, and `paused`. The engine computes `paused` from the
`<branch-key>.control.json` pause flag, so the sandbox needs no file access.

## 4. The simulation

`sliceme status --simulate` groups recorded records into DAG waves,
materializes each wave's combined tree, and runs the configured checks once over
the combined result. `--no-checks` plans only. The simulation is a dry run: it
changes no record, no branch, and no base branch.

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
- **Rebuildable.** Node statuses derive from the recorded records in
  `state.db` and the adapter's session descriptor. Git and `state.db` win on
  conflict, so a stale `status` read self-corrects on the next call.
- **Private.** The check output, the report, and the evidence document stay
  under the git-excluded `.sliceme/`.

## 7. Tests

- `tests/test_status_summary.py` drives the dense `status` summary.
- `tests/test_checks.py` drives the check cache and `status` check counts.
- `tests/test_sessions.py` covers the `verdicts` columns and the migration.
- `tests/test_evidence.py` drives the evidence document.
- `tests/test_cli.py` drives the `status` modes.
- `tests/test_pi_package.py` checks the wiring and the resource grants.

## 8. Related work

- `docs/sessions.md` describes suspend and resume.
- `docs/database.md` documents the schema.
- `docs/reference.md` documents the `status`, `ready`, `check`, `report`, and
  `evidence` actions.
