# Sliceme database design

Status: current.

This document describes the local SQLite database of the plane. It covers the
location, the connection, the schema evolution, every table and column, the row
lifecycle, and the invariants. Those invariants keep the database a consistent
audit trail.

Implementation: `sliceme/store.py`. Business rules live in `sliceme/service.py`,
`sliceme/integrate.py`, and `sliceme/executor.py`; this module owns persistence
only. `docs/reference.md` §3 describes the plane directory layout.

## 1. Scope

The database holds what git and the plane files cannot express quickly:

- the work units and the commits they offer;
- the single executor's check queue, its fingerprints, and its results;
- the metrics of each subagent try;
- the review decisions and comments.

It deliberately does **not** hold the plan. `dag.json` and `state.json` are
files under `.sliceme/`; the plan is authoritative there, and the engine reads
it. `state.json` is a rebuildable cache, and git plus this database win on
conflict. The suspend/resume descriptor is a file too; the database keeps no
projection of it.

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

WAL lets many readers proceed while one writer holds the write lock, which
suits a coordinator, parallel subagents, and a second terminal all reading.
`busy_timeout` lets a writer wait rather than fail immediately when another
process holds the lock. The review server opens each plane's `Store` once to run
the schema, then request threads open with `migrate=False`.

## 3. Schema evolution

Schema creation is idempotent: every statement uses `CREATE TABLE IF NOT
EXISTS` and `CREATE INDEX IF NOT EXISTS`. Opening an older plane adds only the
missing parts.

Structural changes go through `Store._migrate`, which calls `_ensure_columns`
to add columns that predate the current schema:

- `jobs.timeout INTEGER NOT NULL DEFAULT 3600`
- `candidates.node TEXT`
- `units.campaign TEXT`
- `candidates.campaign TEXT`
- `jobs.campaign TEXT`
- `attempts.campaign TEXT`

The pattern is additive only. New columns must have a default or be nullable,
because existing rows are not rewritten. Destructive changes (renames, drops,
type changes) are deliberately avoided so a running or older plane keeps
working.

## 4. Entity relationships

```text
campaigns 1 ──── * units
campaigns 1 ──── * candidates

jobs              (standalone; carries campaign)
attempts          (standalone; carries campaign)
review_decisions  (standalone; keyed by branch_key and commit)
comments          (standalone; keyed by branch_key)
```

- A **campaign** is one target branch, one campaign worktree branch, one DAG,
  and one review queue. A plane holds one or more campaigns.
- A **unit** is one writer: a git worktree plus a branch. A campaign unit
  belongs to its campaign; `start` may create one more unit for a directory.
- A **candidate** is a committed head a unit offers for delivery. The campaign
  worktree accumulates commits, so a later wave adds rows.
- A **job** is a check vector queued for the single executor. It carries its own
  fingerprint and result; the review evidence reads the newest terminal job for
  a commit.
- A **try** holds one subagent run and its metrics.
- A **review decision** is one approval or rejection for one commit.
- A **comment** is one review comment on a commit or the report.

All `*_at` columns are `REAL` epoch seconds from `util.now()` (`time.time()`).

## 5. Table reference

### 5.1 `units`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `name` | TEXT NOT NULL | `campaign`, or a `start` unit name |
| `kind` | TEXT NOT NULL DEFAULT `'worker'` | `worker` or `campaign` |
| `worktree` | TEXT NOT NULL | absolute path under `.sliceme/worktrees/` |
| `branch` | TEXT NOT NULL | `sliceme/<name>` (or the campaign worktree branch) |
| `base_commit` | TEXT | fork point |
| `state` | TEXT NOT NULL DEFAULT `'working'` | `working`, then `landed` |
| `created_at` | REAL NOT NULL | |
| `updated_at` | REAL NOT NULL | |
| UNIQUE(name) | | |

Created by `Service.create_workspace` and `Service.create_campaign_workspace`
after `gitutil.add_worktree` succeeds; if the insert fails the worktree is
cleaned up. Updated by `Store.set_unit_state` (via `integrate._mark_delivered`,
which sets `landed`). Read by `list_units`, `get_unit`, `require_unit`,
`Service.current_unit` (worktree containment, else branch match),
`Service.status`, `campaign.build_skeleton`, and `Service.gc`.

`gc` prunes worktrees and branches for units in state `landed` or `closed`. Only
`landed` occurs today; Sliceme reserves `closed`.

### 5.2 `candidates`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `unit_id` | INTEGER NOT NULL → `units(id)` | |
| `head_commit` | TEXT NOT NULL | the node commit on the campaign branch |
| `status` | TEXT NOT NULL DEFAULT `'prepared'` | `prepared`, `pending`, `landed`, `failed`, `blocked` |
| `summary` | TEXT | review text from `wave --record --summary` |
| `node` | TEXT | DAG node id (migration) |
| `created_at` | REAL NOT NULL | |
| `updated_at` | REAL NOT NULL | |

Index: `idx_candidates_status`.

Created by `Service._record_wave_commits`, one per changed node in a wave (with
`node` set). The campaign worktree accumulates commits, so a later wave adds
rows and never rewrites an earlier wave's row. The unit join provides the branch
and the base commit, so the table does not duplicate them.

Updated by `integrate._mark_delivered`, which sets `status='landed'` without
rewriting `head_commit` (the node commit is provenance).

Read by `Store.list_candidates` and `Store.get_candidate` (both join `units` to
add `unit_name`, `unit_branch`, `worktree`, and `unit_base_commit`),
`plan_waves`, `Service.status`, `Service.review_snapshot`, and
`campaign.build_skeleton`.

### 5.3 `jobs`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `wave` | INTEGER | campaign wave |
| `requester` | TEXT | verifier id |
| `source` | TEXT NOT NULL | `node:<id>` or `wave:<n>` |
| `commit_ref` | TEXT NOT NULL | commit the checks run at |
| `tree` | TEXT | git tree hash |
| `commands` | TEXT NOT NULL | JSON command vector |
| `sandbox` | TEXT | JSON sandbox profile |
| `sandbox_digest` | TEXT | folded into the fingerprint |
| `gpu` | TEXT NOT NULL DEFAULT `'none'` | `none`, `T1`, `T2` |
| `priority` | INTEGER NOT NULL DEFAULT 0 | higher runs first |
| `status` | TEXT NOT NULL DEFAULT `'queued'` | `queued`, `running`, `passed`, `failed`, `error`, `cancelled` |
| `fingerprint` | TEXT | text fingerprint, not a foreign key |
| `timeout` | INTEGER NOT NULL DEFAULT 3600 | per-command timeout (migration) |
| `requested_at` | REAL NOT NULL | enqueue time |
| `started_at` | REAL | claim time |
| `finished_at` | REAL | completion time |
| `duration` | REAL | measured run time |
| `exit_code` | INTEGER | first non-zero command exit |
| `output` | TEXT | formatted check output |
| `error` | TEXT | failure text |
| `runner_pid` | INTEGER | process that claimed the job |

Indexes: `idx_jobs_status`, `idx_jobs_fingerprint`.

Created by `Executor.submit` through `Store.create_job` after it resolves the
sandbox and computes the fingerprint. `Store.find_passed_job` short-circuits a
submit whose fingerprint already passed, so no new row appears on a cache hit.

Updated by `Executor.run_job` (marks `running` with `started_at` and
`runner_pid`, then terminal with `finished_at`, `duration`, `exit_code`,
`output`, and `error`), by `Executor.cancel`, and by
`Store.recover_orphan_jobs`, which returns expired `running` rows to `queued`.

Claimed by `Store.claim_next_job`, which selects the highest-priority queued row
and marks it `running` inside one transaction, so two runners can never take the
same job.

Read by `Executor.status` (`job_counts`, queued, running, recent rows),
`Executor.wait`, `Service.status`, and `Store.latest_job_for_commit`, which is
the review evidence for a commit.

### 5.4 `attempts`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `node` | TEXT NOT NULL | DAG node id |
| `unit` | TEXT | unit name |
| `attempt` | INTEGER NOT NULL DEFAULT 1 | attempt number |
| `agent` | TEXT NOT NULL DEFAULT `'worker'` | `worker`, `planner`, `verifier` |
| `status` | TEXT NOT NULL DEFAULT `'running'` | `running`, then `ok`, `failed`, `cancelled` |
| `started_at` | REAL NOT NULL | |
| `finished_at` | REAL | |
| `duration` | REAL | |
| `exit_code` | INTEGER | |
| `turns` | INTEGER NOT NULL DEFAULT 0 | |
| `tool_calls` | INTEGER NOT NULL DEFAULT 0 | |
| `tools` | TEXT | JSON tool histogram |
| `tool_seconds` | REAL NOT NULL DEFAULT 0 | total tool call time |
| `tool_durations` | TEXT | JSON map of tool name to total seconds |
| `slowest_commands` | TEXT | JSON list of the slowest shell commands |
| `tokens_in` | INTEGER NOT NULL DEFAULT 0 | |
| `tokens_out` | INTEGER NOT NULL DEFAULT 0 | |
| `cost` | REAL NOT NULL DEFAULT 0 | approximate cost |
| `last_tool` | TEXT | |
| `last_activity_at` | REAL | |
| `error` | TEXT | |

Indexes: `idx_attempts_node`, `idx_attempts_status`.

Created by `Store.create_attempt` through `Service.begin_attempt` (the
`attempt --begin` action). Finished by `Store.finish_attempt` through
`Service.end_attempt` (`attempt --end`). Read by `Service.attempts`, the
`Service.progress` projection, and the resume continuation prompt. The time
split is the attempt wall clock minus `tool_seconds`, and the result is the
thinking time (`docs/observability.md`).

### 5.5 `review_decisions`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `branch_key` | TEXT NOT NULL | feature-branch key (`feat/x` → `feat--x`) |
| `commit_hash` | TEXT | the reviewed commit; null is a campaign-level override |
| `action` | TEXT NOT NULL | `approve`, `request_changes`, `override` |
| `actor` | TEXT | who recorded the decision |
| `note` | TEXT | required for `override` |
| `created_at` | REAL NOT NULL | |
| `consumed_at` | REAL | set by a successful delivery |

Index: `idx_review_decisions_commit(branch_key, commit_hash, id)`.

Append-only. `Store.add_review_decision` inserts; the newest row for a commit
wins. `Store.latest_decisions_by_commit` reads the newest row per commit, and
`Store.consume_review_decisions` sets `consumed_at` after a successful delivery.
An approval binds to one commit hash, so a later commit re-opens the gate and an
approval can never outlive the diff it approved. A failed check or forge call
leaves the rows unconsumed, so a retry needs no new review.

### 5.6 `comments`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `branch_key` | TEXT NOT NULL | feature-branch key |
| `commit_hash` | TEXT | the commit the comment attaches to; null for the report |
| `file` | TEXT | file path |
| `side` | TEXT | `old` or `new` |
| `line` | INTEGER | start line |
| `line_end` | INTEGER | end line for a range |
| `body` | TEXT NOT NULL | the comment text |
| `node` | TEXT | DAG node attribution |
| `status` | TEXT NOT NULL DEFAULT `'open'` | `open`, `delivered`, `addressed` |
| `created_at` | REAL NOT NULL | |
| `addressed_at` | REAL | when the coordinator addressed it |

Index: `idx_comments_branch(branch_key, status, id)`.

Created by `Service.review_comment`. `Service.review_poll` reads the `open` rows
for the relay; `Service.review_ack` sets `delivered`. The queue is at-least-once:
a lost ack repeats a comment, never loses it.

### 5.7 `campaigns`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `key` | TEXT NOT NULL UNIQUE | `branch_key(target_branch)` |
| `target_branch` | TEXT NOT NULL UNIQUE | the feature branch delivery lands on |
| `worktree_branch` | TEXT NOT NULL UNIQUE | the campaign accumulation branch |
| `base` | TEXT | fork point |
| `unit_name` | TEXT NOT NULL UNIQUE | the campaign worktree unit name |
| `name` | TEXT | display name |
| `design` | TEXT | design document reference |
| `state` | TEXT NOT NULL DEFAULT `'working'` | `working`, `delivered`, `closed` |
| `pr_url` | TEXT | the delivery pull request URL |
| `pr_number` | INTEGER | the delivery pull request number |
| `created_at` | REAL NOT NULL | |
| `updated_at` | REAL NOT NULL | |

Index: `idx_campaigns_state(state)`.

Created by `Service._ensure_legacy_campaign`, `Service._sync_campaign_retarget`,
and `Store.create_campaign` (idempotent on the key or target). Read by
`Service.campaign`, `Service.status`, the review server, and the report. A
legacy plane gets one row on first open. `Service.deliver` sets `delivered` and
records `pr_url` / `pr_number`.

## 6. Row lifecycle

1. **Plane bootstrap.** `start` writes `.sliceme/config.json` and opens
   `Store`, which creates the schema. Sliceme registers one campaign for the
   recorded target branch.
2. **Unit.** `start` or `wave --open` creates a worktree and branch, then
   inserts a `units` row with `state='working'`.
3. **Wave record.** `wave --record --wave N` runs conformance-by-ownership on
   the campaign worktree and inserts one `candidates` row per changed node with
   `status='prepared'`.
4. **Verification.** The executor inserts `jobs` rows from `exec --submit` and
   updates them through `exec --run`; the cache serves a passing fingerprint.
   `Service.review_snapshot` reads the newest terminal job per commit as
   evidence.
5. **Delivery.** When every wave completes and a human approves every commit,
   `deliver` pushes the campaign worktree branch and opens a pull request, then
   `_mark_delivered` sets the candidates and their unit to `landed`.
6. **Review.** The browser writes `comments` and `review_decisions`; the pi
   relay reads `open` comments and marks them `delivered`.
7. **Dashboard and report.** `Service.status` reads units, candidates, waves,
   and job counts. `campaign.build_skeleton` reads units, candidates, and the
   newest job per candidate for the report.
8. **Cleanup.** `Service.gc` reads `landed`/`closed` units and removes their
   worktrees and branches, and prunes expired review rows. Rows are never
   deleted otherwise; the tables remain the audit trail.

## 7. Access summary

| Table | Writers | Readers |
|---|---|---|
| `campaigns` | `Service._ensure_legacy_campaign`, `_sync_campaign_retarget`, `Store.create_campaign`, `Service.deliver` | `Service.campaign`, `Service.status`, the review server |
| `units` | `Service.create_workspace`, `create_campaign_workspace`, `integrate._mark_delivered`, `gc` (branch prune) | `Service.status`, `current_unit`, `unit_detail`, `gc`, `campaign.build_skeleton` |
| `candidates` | `Service._record_wave_commits`, `integrate._mark_delivered` | `Service.status`, `Service.review_snapshot`, `campaign.build_skeleton` |
| `jobs` | `Executor.submit/run_job/cancel`, `recover_orphan_jobs` | `Executor.status/wait`, `Service.status`, `Service.review_snapshot` |
| `attempts` | `Service.begin_attempt`, `end_attempt` | `Service.attempts`, the resume prompt |
| `review_decisions` | `Service.review_decision`, `consume_approvals` | `Service.review_snapshot`, `require_all_approved` |
| `comments` | `Service.review_comment`, `review_ack` | `Service.review_poll`, `review_snapshot` |

## 8. Status and value domains

| Table | Column | Values |
|---|---|---|
| `campaigns` | `state` | `working` (default), `delivered`, `closed` |
| `units` | `state` | `working` (default), `landed`; `closed` reserved |
| `units` | `kind` | `worker` (default), `campaign` |
| `candidates` | `status` | `prepared` (default), `pending`, `landed`, `failed`, `blocked` |
| `jobs` | `status` | `queued` (default), `running`, `passed`, `failed`, `error`, `cancelled` |
| `jobs` | `gpu` | `none`, `T1`, `T2` |
| `attempts` | `status` | `running` (default), `ok`, `failed`, `cancelled` |
| `review_decisions` | `action` | `approve`, `request_changes`, `override` |
| `comments` | `status` | `open` (default), `delivered`, `addressed` |

## 9. Concurrency and integrity

- `Store.tx()` commits on success and rolls back on exception. Methods that
  write outside it rely on the caller to `self.store.conn.commit()`; the
  executor commits explicitly after recording results.
- `claim_next_job` and `recover_orphan_jobs` use `tx()`, so job state
  transitions are atomic.
- Foreign keys are on. There are no cascade rules, and Sliceme deletes no rows,
  so referential integrity holds by construction rather than by cleanup.
- WAL plus `busy_timeout` allows a coordinator, subagents, and a second
  terminal to read the same plane safely.
- The unique constraints that matter for correctness are `units.name` and
  `jobs.fingerprint` with `status='passed'` (the cache lookup).

## 10. What is not in the database

- `dag.json` (the authored plan) and `state.json` (per-node status cache) are
  files under `.sliceme/`. The coordinator owns writing them; Python reads
  them. `state.json` is rebuildable, and git plus the database win on conflict.
- The suspend/resume descriptor `.sliceme/<branch-key>.session.json` is a file.
  `status --sessions` and `status --resume` read it directly; there is no
  `campaign_sessions` projection.
- `events.jsonl` is the extension's append-only audit log.
- Worker logs are files, one per node.
- The report is a Markdown file, included in the review snapshot.

## 11. Suspend/resume and subagent tries

`attempts` persists per-subagent timings and agent metrics (turns, tool calls,
tokens, cost, last tool, last activity), written through the `attempt --begin` /
`--end` action. It follows the additive-migration approach used for
`jobs.timeout` and `candidates.node`.

The suspend/resume descriptor is the pi adapter's file. The engine reads it in
`status --resume` and `status --sessions` and keeps no database copy, so the
descriptor is the single source of truth.

## 12. Local review

`review_decisions` is an append-only approval log: the newest row for a commit
wins, and `consumed_at` marks a delivery that already used the approval. The row
binds to one commit hash, so `Service.deliver` refuses until a human approves
every accumulated commit. `comments` is the review queue: the pi relay reads
`open`
rows with `review --poll`, sends them to the coordinator session, and marks them
`delivered` with `review --ack`. `gc` prunes rows for campaigns without a
descriptor older than the retention window (`policy.review_retention_days`,
default 30) and never prunes the current campaign. See `docs/review.md` for the
design.
