# Sliceme observability

Status: implemented. This document describes what the engine reports about a
campaign, and where child-run state lives. See
[directory-slices-plan.md](./directory-slices-plan.md) for the one-node-per-directory
target design.

## 1. Purpose

While a campaign runs, the coordinator and a second terminal answer these
questions from the engine:

1. How many phases and nodes exist, and which phase is current?
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
  evidence) and `.sliceme/<branch-key>.evidence.md` (the bounded Markdown),
  written by `evidence`. The pull request body is the shorter summary document,
  not the evidence document.

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
one line per node in DAG order, and one line per phase. Its node and phase lines
come from `dag.json`, git, the recorded records in `state.db`, and the
session descriptor. The
header shows the work branch, the worktree path, and `base` from the campaign
record and the plan, so a second terminal sees the same state.

```text
campaign:    dense
work branch: feat/x  worktree: /repo/.worktrees/feat-x  base: main
design:      DESIGN.md
nodes:       2  phase size: 2
phases:      2  current: 1
  w0 w1 [build] done — parser
  w1 w2 [-] running
phase 0 [done]: w1
phase 1 [pending]: w2
```

`--dense` asks for it explicitly; `--verbose` prints the full nested dump. With
`--json` the default is the nested dump, while `--dense --json` emits the
summary as JSON (the `lines` array holds the same text). A plane with no single
campaign falls back to a compact per-campaign plane list.

The `phases:` line names the planned phase count and the current phase. The
current phase is the scheduler's cursor. It is the first phase that still has
an unrecorded member, or the last phase when every member has a record. It
holds the same value as the `phase` key of `sliceme ready`. A campaign with a
record for every phase adds `(done)` to the marker. The JSON form also carries
`phase_count` and `current_phase`.

### 3.2 The nested dump

`status --verbose` or `status --json` returns the nested view:

- `dag_phases` (the scheduler's phase plan) and `dag_phases_error`;
- `ready` (the ready node ids), `paused`, `pause_reason`, and `budget`;
- `checks` (the terminal-check counts);
- the campaign writer and `records` with their `node`, `log`, `record`,
  and `verification` columns;
- `sandbox` (the resolved gate);
- `dag_merge` (the same-ownership contraction).

### 3.3 The loop read

`ready` returns the small object the workflow resource polls: `campaign`,
`ready`, `nodes`, `dag`, `phase`, `paused`, `pause_reason`, and `budget`. The
resource reads the brief form (`ready --brief`), so `nodes` carries no goal or
tasks and one reply stays under the 4096-byte standard output cap of the host.
The engine computes `paused` from the `<branch-key>.control.json` pause flag, so
the sandbox needs no file access. The pi adapter writes the flag with
`reason: user` for `/suspend`, or `reason: budget` when a wall-clock or cost cap
trips.

## 4. Where child state lives

The `sliceme.campaign` workflow resource launches each child through
pi-subagents. pi-subagents owns the child status, the event stream, the tool
scoping, and the run control. The engine receives only JSON from `runs.host`,
so it never reads a child's live state.

A phase with a stalled or failed child shows up as a node that never reaches
`recorded`. The coordinator reads `ready` again after the phase children settle,
and the next `record` attributes the changes that exist.

## 5. Constraints

- **No daemon.** Poll `sliceme status` or `ready`. Do not add `--follow`.
- **One synchronous runner.** The check runner writes one terminal row per run,
  so the count stays exact.
- **Rebuildable.** Node statuses derive from the recorded records in
  `state.db` and the adapter's session descriptor. Git and `state.db` win on
  conflict, so a stale `status` read self-corrects on the next call.
- **Private.** The check output, the report, and the evidence document stay
  under the git-excluded `.sliceme/`.

## 6. Tests

- `tests/test_status_summary.py` drives the dense `status` summary.
- `tests/test_checks.py` drives the check cache and `status` check counts.
- `tests/test_sessions.py` covers the `verdicts` columns and the migration.
- `tests/test_evidence.py` drives the evidence document.
- `tests/test_cli.py` drives the `status` modes.
- `tests/test_pi_package.py` checks the wiring and the resource grants.

## 7. Related work

- `docs/sessions.md` describes suspend and resume.
- `docs/database.md` documents the schema.
- `docs/reference.md` documents the `status`, `ready`, `check`, `report`, and
  `evidence` actions.
