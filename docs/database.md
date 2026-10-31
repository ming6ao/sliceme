# Sliceme database design

Status: current.

This document describes the local SQLite database of the plane. It covers the
location, the connection, the schema evolution, every table and column, the row
lifecycle, and the invariants. Those invariants keep the database a consistent
audit trail.

Implementation: `sliceme/store.py`. Business rules live in the `sliceme/verbs/`
groups and in `sliceme/integrate.py` and `sliceme/checks.py`; this module owns
persistence only. `docs/reference.md` §3 describes the plane directory layout.

## 1. Scope

The database holds what git and the plane files cannot express quickly:

- the work units and the commits they offer;
- the one check runner's fingerprints and terminal results;
- the campaign review decisions;
- the timing steps of the campaign child runs.

It deliberately does **not** hold the plan. `dag.json` is a file under
`.sliceme/`; the plan is authoritative there, and the engine reads it. Node
statuses live in the adapter-written session descriptor; there is no
`state.json`, and git plus this database win on conflict. The suspend/resume
descriptor is a file too; the database keeps no projection of it.

Child execution state belongs to pi-subagents. The database keeps no `jobs`,
`attempts`, or `comments` table.

## 2. Location and connection

- **Path:** `.sliceme/state.db` in the plane root (`util.db_path`).
- **Engine:** SQLite in write-ahead logging (WAL) mode.
- **Open** (`Store.__init__`):
  - `sqlite3.connect(path, timeout=10.0)`;
  - `row_factory = sqlite3.Row`;
  - `PRAGMA journal_mode=WAL`;
  - `PRAGMA foreign_keys=ON`;
  - `PRAGMA busy_timeout=5000`;
  - `executescript(SCHEMA)`, then `_migrate()`, then `commit()`.

WAL lets many readers proceed while one writer holds the write lock, which suits
a coordinator, parallel children, and a second terminal all reading.
`busy_timeout` lets a writer wait rather than fail at once when another process
holds the lock.

## 3. Schema evolution

Schema creation is idempotent: every statement uses `CREATE TABLE IF NOT
EXISTS` and `CREATE INDEX IF NOT EXISTS`. Opening an older plane adds only the
missing parts.

Structural changes go through `Store._migrate`, which calls `_ensure_columns`
to add columns that predate the current schema:

- `campaigns.pr_url`, `campaigns.pr_number`
- `campaigns.base_branch` (backfilled once from `found_default_branch`)
- `campaigns.worktree` (backfilled once from the retired campaign unit)
- `records.node`
- `records.campaign`
- `verdicts.phase`

The `verdicts.phase` entry restores a column that the schema once named
`wave`. The migration renames `wave` to `phase` when `wave` exists. It adds a
nullable `phase` when neither column exists, so old rows keep their value and
new rows can write the phase. The retired `gpu` column stays; it is harmless.
The legacy `checks` to `verdicts` copy reads `wave` or `phase` from the source
table when the source has one.

The pattern is additive only for columns: new columns must have a default or be
nullable, because existing rows are not rewritten. One destructive migration is
allowed: `Store._drop_unit_schema` retires the `units` table and rebuilds
`records` without the dropped `unit_id` foreign key (after backfilling
`campaigns.worktree`).

## 4. Entity relationships

```text
campaigns 1 ──── * records

verdicts          (standalone; carries campaign)
approvals  (standalone; keyed by branch_key)
```

- A **campaign** is one work branch (the pull request head), one base branch
  (the pull request base), one worktree, one DAG, and one confirmation gate.
  A plane holds one or more campaigns.
- A **record** is a committed head the campaign offers for delivery. The
  campaign worktree accumulates commits, so a later phase adds rows.
- A **check** is one terminal check result. It carries its own fingerprint and
  result; the review evidence reads the newest terminal check for a commit.
- A **review decision** is one campaign-level approval. `deliver` records it
  after the human confirmation and consumes it when the delivery lands.

All `*_at` columns are `REAL` epoch seconds from `util.now()` (`time.time()`).

## 5. Table reference

### 5.1 `records`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `campaign` | TEXT | the owning campaign key (migration) |
| `head_commit` | TEXT NOT NULL | the node commit on the work branch |
| `status` | TEXT NOT NULL DEFAULT `'recorded'` | `recorded`, `pending`, `delivered`, `failed`, `blocked` |
| `summary` | TEXT | review text recorded with the node commit |
| `node` | TEXT | DAG node id (migration) |
| `empty` | INTEGER NOT NULL DEFAULT 0 | 1 for an explicit empty record (the member changed no file) |
| `created_at` | REAL NOT NULL | |
| `updated_at` | REAL NOT NULL | |

Indexes: `idx_records_status`, `idx_records_campaign`.

Created by `Service._record_phase_commits`, one per phase member (`node` set): a
commit record for each changed node and an empty marker for a member that
changed no file. The campaign worktree accumulates commits, so a later phase adds
rows and never rewrites an earlier phase's row. The campaign owns the branch and
the worktree, so the table duplicates neither.

Updated by `integrate._mark_delivered`, which sets `status='delivered'` without
rewriting `head_commit` (the node commit is provenance).

Read by `Store.list_records` and `Store.get_record`, `integrate.plan_phases`,
`Service.status`, `packet.build_packet`, and `campaign.build_skeleton`.

### 5.2 `verdicts`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `fingerprint` | TEXT NOT NULL | content fingerprint; the cache key |
| `phase` | INTEGER | campaign phase |
| `campaign` | TEXT | the owning campaign key |
| `source` | TEXT NOT NULL | `phase:<n>` or `deliver` |
| `commit_ref` | TEXT NOT NULL | commit the checks run at |
| `tree` | TEXT | git tree hash |
| `commands` | TEXT NOT NULL | JSON command vector |
| `checks` | TEXT | JSON list of `{name, command, required, timeout}` |
| `sandbox` | TEXT | JSON sandbox profile |
| `sandbox_digest` | TEXT | folded into the fingerprint |
| `status` | TEXT NOT NULL | `passed`, `failed`, or `error` |
| `duration` | REAL | measured run time |
| `exit_code` | INTEGER | first non-zero command exit |
| `output` | TEXT | formatted check output |
| `results` | TEXT | JSON per-check results |
| `error` | TEXT | failure text |
| `created_at` | REAL NOT NULL | row write time |
| `finished_at` | REAL | completion time |

Indexes: `idx_verdicts_fingerprint`, `idx_verdicts_campaign`,
`idx_verdicts_commit(commit_ref, id)`.

Created by `CheckRunner.run` through `Store.create_verdict` after it resolves the
sandbox and computes the fingerprint. The cache lookup short-circuits a run
whose fingerprint already reached a terminal verdict (`passed`, `failed`, or
`error`), so no new row appears on a cache hit.

The row is a **cache, not a queue**. It holds no lease and no runner state. The
`cancelled` and `running` statuses do not exist: one run writes one terminal row.

Read by `Store.find_verdict` (the cache lookup), `Store.get_verdict` (`check`),
`Store.latest_verdict_for_commit` (the review evidence),
`packet._evidence_map`, and `Store.verdict_counts` (`status`).

### 5.3 `approvals`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `branch_key` | TEXT NOT NULL | work-branch key (`feat/x` → `feat--x`) |
| `commit_hash` | TEXT | null for a campaign-level decision |
| `action` | TEXT NOT NULL | `approve` (the only value `deliver` writes) |
| `actor` | TEXT | who confirmed the delivery |
| `note` | TEXT | optional approval note |
| `created_at` | REAL NOT NULL | |
| `consumed_at` | REAL | set by a successful delivery |

Index: `idx_approvals_commit(branch_key, commit_hash, id)`.

Append-only. `Store.add_approval` inserts; the newest row for a campaign
wins. `Store.latest_approval` reads the newest row, and
`Store.consume_approvals` sets `consumed_at` after a successful delivery.
One approval covers the whole campaign commit set, so a later commit does not
re-open the gate. `Service.record_delivery_approval` writes the row inside
`deliver --confirm`. A failed check or forge call leaves the row unconsumed,
and the next delivery appends a new row.

### 5.4 `campaigns`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `key` | TEXT NOT NULL UNIQUE | `branch_key(work_branch)` |
| `work_branch` | TEXT NOT NULL UNIQUE | the work branch (the pull request head) |
| `worktree` | TEXT | the campaign's one worktree path (backfilled from the retired campaign unit) |
| `base_branch` | TEXT | the pull request base (the default branch; migration) |
| `unit_name` | TEXT NOT NULL UNIQUE | the campaign worktree writer name |
| `name` | TEXT | display name |
| `design` | TEXT | design document reference |
| `state` | TEXT NOT NULL DEFAULT `'working'` | `working`, `delivered`, `landed`, `closed` |
| `delivery_mode` | TEXT | `pull_request` or `local` (migration; NULL means `pull_request`) |
| `merge_commit` | TEXT | the local merge commit on the base branch (migration) |
| `pr_url` | TEXT | the delivery pull request URL (migration) |
| `pr_number` | INTEGER | the delivery pull request number (migration) |
| `created_at` | REAL NOT NULL | |
| `updated_at` | REAL NOT NULL | |

Index: `idx_campaigns_state(state)`.

Created by `Service._ensure_legacy_campaign`, `BootstrapVerbs._create_plane_campaign`,
and `Store.create_campaign` (idempotent on the key or target). Read by
`Service.campaign`, `Service.status`, the report and evidence, and `plan`. A
legacy plane gets one row on first open. `Service.deliver` sets `delivered` for
a `pull_request` delivery and `landed` for a `local` delivery. It records
`pr_url` / `pr_number` for a pull request and `merge_commit` for a local merge.

### 5.5 `timings`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `campaign` | TEXT | the owning campaign key |
| `step_id` | TEXT NOT NULL | `run:thinking:<n>` or `run:tool:<call id>` |
| `run` | TEXT | the pi-subagents run id |
| `agent` | TEXT | `sliceme-planner`, `sliceme-worker`, or `reviewer` |
| `child_index` | INTEGER NOT NULL DEFAULT 0 | the child index of the run |
| `kind` | TEXT NOT NULL | `thinking` or `tool` |
| `tool` | TEXT | the tool name for a tool step |
| `command` | TEXT | the bounded command text (bash only) |
| `command_truncated` | INTEGER NOT NULL DEFAULT 0 | 1 when the store cut the command |
| `is_error` | INTEGER NOT NULL DEFAULT 0 | 1 for a failed tool call |
| `duration` | REAL NOT NULL | measured seconds |
| `started_at` | REAL | start time (epoch seconds) |
| `ended_at` | REAL | end time (epoch seconds) |
| `source` | TEXT | the child transcript path |
| `created_at` | REAL NOT NULL | row write time |

Unique key: `(campaign, step_id)`. Indexes: `idx_timings_campaign`,
`idx_timings_kind(campaign, kind, duration)`.

Created by `timings.ingest` from the pi-subagents child transcripts. The
engine ingests at `record`, `check`, and `report`, so the rows survive the
seven-day artifact cleanup. A thinking step is one assistant message with the
model-call duration. A tool step is one `tool_start`/`tool_end` pair. The
unique key makes a repeated ingest idempotent.

Read by `timings.summarize` and `Service.report`. The rows are a derived
snapshot, not child execution state: pi-subagents remains the owner of a live
run. `gc` keeps the rows with the report, and `remove_campaign_artifacts`
deletes them only when it removes the report.

## 6. Row lifecycle

1. **Plane bootstrap.** `start` writes `.sliceme/config.json` and opens
   `Store`, which creates the schema. Sliceme registers one campaign for the
   recorded work branch and base branch.
2. **Worktree.** `open` creates the campaign worktree and branch and
   records the path on the campaign row (`campaigns.worktree`).
3. **Phase record.** `record` runs conformance-by-ownership on
   the campaign worktree and inserts one `records` row per changed node with
   `status='recorded'`.
4. **Check.** `check` calls the one runner. The runner inserts a
   `verdicts` row, or the cache serves a terminal fingerprint. `packet.build_packet`
   reads the newest terminal check per commit as evidence.
5. **Delivery.** After every phase completes and the user confirms, `deliver`
   runs in the selected mode. A `pull_request` delivery pushes the work branch
   and opens a pull request against the delivery base. A `local` delivery
   merges the work branch into the local base branch. Then `_mark_delivered`
   sets the campaign's records to `delivered` and the campaign state to
   `delivered` or `landed`.
6. **Review.** The human confirms the campaign one time. `deliver --confirm`
   records one campaign approval, and a landed delivery consumes the row.
7. **Status and report.** `Service.status` reads the campaign row, records,
   phases, and check counts. `campaign.build_skeleton` reads the campaign row,
   records, and the newest verdict per record for the report.
8. **Cleanup.** `Service.gc` reads the campaign registry and removes the
   worktree and branch of campaigns in `landed`/`delivered`/`closed`, and prunes
   expired review rows. Rows are never deleted otherwise; the tables remain the
   audit trail.

## 7. Access summary

| Table | Writers | Readers |
|---|---|---|
| `campaigns` | `Service._ensure_legacy_campaign`, `BootstrapVerbs._create_plane_campaign`, `Store.create_campaign`, `Store.set_campaign_worktree`, `Service.deliver` | `Service.campaign`, `Service.status`, `plan`, the report and evidence, `gc` |
| `records` | `Service._record_phase_commits`, `integrate._mark_delivered` | `Service.status`, `packet.build_packet`, `campaign.build_skeleton` |
| `verdicts` | `CheckRunner.run` | `Store.find_verdict/get_verdict/latest_verdict_for_commit/verdict_counts`, `packet._evidence_map` |
| `approvals` | `Service.record_delivery_approval`, `Store.consume_approvals` | `Service.campaign_approval`, `Service.campaign_approved`, `packet.build_packet` |

## 8. Status and value domains

| Table | Column | Values |
|---|---|---|
| `campaigns` | `state` | `working` (default), `delivered`, `closed` |
| `records` | `status` | `recorded` (default), `pending`, `delivered`, `failed`, `blocked` |
| `verdicts` | `status` | `passed`, `failed`, `error` |
| `approvals` | `action` | `approve` |

## 9. Concurrency and integrity

- `Store.tx()` commits on success and rolls back on exception. Methods that
  write outside it rely on the caller to `self.store.conn.commit()`; the check
  runner commits after it records a row.
- Foreign keys are on. There are no cascade rules, and Sliceme deletes no rows,
  so referential integrity holds by construction rather than by cleanup.
- WAL plus `busy_timeout` allows a coordinator, children, and a second terminal
  to read the same plane safely.
- The unique constraints that matter for correctness are `campaigns.key`,
  `campaigns.work_branch`.
- The `verdicts` index on `fingerprint` makes the cache lookup cheap.

## 10. What is not in the database

- `dag.json` (the authored plan) is a file under `.sliceme/`. The human writes
  it, and the engine reads it. Node statuses live in the adapter-written session
  descriptor; there is no `state.json`, and git plus the database win on
  conflict.
- The suspend/resume descriptor `.sliceme/<branch-key>.session.json` is a file.
  `sessions` and `resume` read it directly; there is no
  `campaign_sessions` projection.
- Child events, per-child progress, and run control belong to pi-subagents.
  Sliceme keeps no `events.jsonl`, no per-node heartbeat, and no `jobs` table.
  The `timings` table is a derived snapshot of finished child steps, not a
  second run-control plane.
- Worker logs are files, one per node.
- The report and the evidence document are Markdown files. The report is
  included in the review packet; the evidence document is the local, complete
  record. The pull request body is the shorter summary and verification.

## 11. Suspend/resume

The suspend/resume descriptor is the pi adapter's file. The engine reads it in
`resume` and `sessions` and keeps no database copy, so the
descriptor is the single source of truth. Resume reconciles from git plus
`state.db`; the `verdicts` table serves the cached verification, so a resumed node
does not re-run a full check.

## 12. Local review

`approvals` is an append-only decision log: the newest row for a campaign
wins, and `consumed_at` marks a delivery that already used the decision. One
approval covers the whole campaign commit set. `Service.deliver` refuses
without the `confirm` token, then records the row and consumes it when the
delivery lands. `gc` prunes rows for campaigns without a
descriptor older than the retention window (`policy.review_retention_days`,
default 30) and never prunes the current campaign. See `docs/review.md` for the
design.
