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
- the campaign review decisions.

It deliberately does **not** hold the plan. `dag.json` is a file under
`.sliceme/`; the plan is authoritative there, and the engine reads it.
`state.json` is an optional, read-only legacy override that the engine never
writes; git plus this database win on conflict. The suspend/resume descriptor is
a file too; the database keeps no projection of it.

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
- `candidates.node`
- `units.campaign`
- `candidates.campaign`

The pattern is additive only. New columns must have a default or be nullable,
because existing rows are not rewritten. Destructive changes (renames, drops,
type changes) are deliberately avoided so a running or older plane keeps
working.

## 4. Entity relationships

```text
campaigns 1 ──── * units
campaigns 1 ──── * candidates

checks            (standalone; carries campaign)
review_decisions  (standalone; keyed by branch_key)
```

- A **campaign** is one target branch, one campaign worktree branch, one DAG,
  and one approval gate. A plane holds one or more campaigns.
- A **unit** is one writer: a git worktree plus a branch. A campaign unit
  belongs to its campaign; `start` may create one more unit for a directory.
- A **candidate** is a committed head a unit offers for delivery. The campaign
  worktree accumulates commits, so a later wave adds rows.
- A **check** is one terminal check result. It carries its own fingerprint and
  result; the review evidence reads the newest terminal check for a commit.
- A **review decision** is one campaign-level approval, rejection, or override.

All `*_at` columns are `REAL` epoch seconds from `util.now()` (`time.time()`).

## 5. Table reference

### 5.1 `units`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `name` | TEXT NOT NULL | `campaign`, or a `start` unit name |
| `campaign` | TEXT | the owning campaign key (migration) |
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
cleaned up. Updated by `Store.set_unit_state` (through
`integrate._mark_delivered`, which sets `landed`). Read by `list_units`,
`get_unit`, `require_unit`, `Service.current_unit` (worktree containment, else
branch match), `Service.status`, `campaign.build_skeleton`, and `Service.gc`.

`gc` prunes worktrees and branches for units in state `landed` or `closed`.
Only `landed` occurs today; Sliceme reserves `closed`.

### 5.2 `candidates`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `unit_id` | INTEGER NOT NULL → `units(id)` | |
| `campaign` | TEXT | the owning campaign key (migration) |
| `head_commit` | TEXT NOT NULL | the node commit on the campaign branch |
| `status` | TEXT NOT NULL DEFAULT `'prepared'` | `prepared`, `pending`, `landed`, `failed`, `blocked` |
| `summary` | TEXT | review text from `wave --record --summary` |
| `node` | TEXT | DAG node id (migration) |
| `created_at` | REAL NOT NULL | |
| `updated_at` | REAL NOT NULL | |

Indexes: `idx_candidates_status`, `idx_candidates_campaign`.

Created by `Service._record_wave_commits`, one per changed node in a wave (with
`node` set). The campaign worktree accumulates commits, so a later wave adds
rows and never rewrites an earlier wave's row. The unit join provides the branch
and the base commit, so the table does not duplicate them.

Updated by `integrate._mark_delivered`, which sets `status='landed'` without
rewriting `head_commit` (the node commit is provenance).

Read by `Store.list_candidates` and `Store.get_candidate` (both join `units` to
add `unit_name`, `unit_branch`, `worktree`, and `unit_base_commit`),
`integrate.plan_waves`, `Service.status`, `packet.build_packet`, and
`campaign.build_skeleton`.

### 5.3 `checks`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `fingerprint` | TEXT NOT NULL | content fingerprint; the cache key |
| `wave` | INTEGER | campaign wave |
| `campaign` | TEXT | the owning campaign key |
| `source` | TEXT NOT NULL | `wave:<n>` or `deliver` |
| `commit_ref` | TEXT NOT NULL | commit the checks run at |
| `tree` | TEXT | git tree hash |
| `commands` | TEXT NOT NULL | JSON command vector |
| `checks` | TEXT | JSON list of `{name, command, required, timeout}` |
| `sandbox` | TEXT | JSON sandbox profile |
| `sandbox_digest` | TEXT | folded into the fingerprint |
| `gpu` | TEXT NOT NULL DEFAULT `'none'` | `none`, `T1`, `T2` |
| `status` | TEXT NOT NULL | `passed`, `failed`, or `error` |
| `duration` | REAL | measured run time |
| `exit_code` | INTEGER | first non-zero command exit |
| `output` | TEXT | formatted check output |
| `results` | TEXT | JSON per-check results |
| `error` | TEXT | failure text |
| `created_at` | REAL NOT NULL | row write time |
| `finished_at` | REAL | completion time |

Indexes: `idx_checks_fingerprint`, `idx_checks_campaign`,
`idx_checks_commit(commit_ref, id)`.

Created by `CheckRunner.run` through `Store.create_check` after it resolves the
sandbox and computes the fingerprint. The cache lookup short-circuits a run
whose fingerprint already reached a terminal verdict (`passed`, `failed`, or
`error`), so no new row appears on a cache hit.

The row is a **cache, not a queue**. It holds no lease and no runner state. The
`cancelled` and `running` statuses do not exist: one run writes one terminal row.

Read by `Store.find_check` (the cache lookup), `Store.get_check` (`check
--current`), `Store.latest_check_for_commit` (the review evidence),
`packet._evidence_map`, and `Store.check_counts` (`status`).

### 5.4 `review_decisions`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `branch_key` | TEXT NOT NULL | target-branch key (`feat/x` → `feat--x`) |
| `commit_hash` | TEXT | null for a campaign-level decision |
| `action` | TEXT NOT NULL | `approve`, `request_changes`, `override` |
| `actor` | TEXT | who recorded the decision |
| `note` | TEXT | required for `request_changes` and `override` |
| `created_at` | REAL NOT NULL | |
| `consumed_at` | REAL | set by a successful delivery |

Index: `idx_review_decisions_commit(branch_key, commit_hash, id)`.

Append-only. `Store.add_review_decision` inserts; the newest row for a campaign
wins. `Store.latest_review_decision` reads the newest row, and
`Store.consume_review_decisions` sets `consumed_at` after a successful delivery.
One approval covers the whole campaign commit set, so a later commit does not
re-open the gate. A failed check or forge call leaves the row unconsumed, so a
retry needs no new review.

### 5.5 `campaigns`

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
| `pr_url` | TEXT | the delivery pull request URL (migration) |
| `pr_number` | INTEGER | the delivery pull request number (migration) |
| `created_at` | REAL NOT NULL | |
| `updated_at` | REAL NOT NULL | |

Index: `idx_campaigns_state(state)`.

Created by `Service._ensure_legacy_campaign`, `Service._sync_campaign_retarget`,
and `Store.create_campaign` (idempotent on the key or target). Read by
`Service.campaign`, `Service.status`, the report, and `plan`. A legacy plane
gets one row on first open. `Service.deliver` sets `delivered` and records
`pr_url` / `pr_number`.

## 6. Row lifecycle

1. **Plane bootstrap.** `start` writes `.sliceme/config.json` and opens
   `Store`, which creates the schema. Sliceme registers one campaign for the
   recorded target branch.
2. **Unit.** `start` or `wave --open` creates a worktree and branch, then
   inserts a `units` row with `state='working'`.
3. **Wave record.** `wave --record --current` runs conformance-by-ownership on
   the campaign worktree and inserts one `candidates` row per changed node with
   `status='prepared'`.
4. **Check.** `check --current` calls the one runner. The runner inserts a
   `checks` row, or the cache serves a terminal fingerprint. `packet.build_packet`
   reads the newest terminal check per commit as evidence.
5. **Delivery.** After every wave completes and a human approves the campaign,
   `deliver` pushes the campaign worktree branch and opens a pull request, then
   `_mark_delivered` sets the candidates and their unit to `landed`.
6. **Review.** The human records one campaign decision through
   `review --decision`. The delivery gate reads the newest unconsumed row.
7. **Status and report.** `Service.status` reads units, candidates, waves, and
   check counts. `campaign.build_skeleton` reads units, candidates, and the
   newest check per candidate for the report.
8. **Cleanup.** `Service.gc` reads `landed`/`closed` units and removes their
   worktrees and branches, and prunes expired review rows. Rows are never
   deleted otherwise; the tables remain the audit trail.

## 7. Access summary

| Table | Writers | Readers |
|---|---|---|
| `campaigns` | `Service._ensure_legacy_campaign`, `_sync_campaign_retarget`, `Store.create_campaign`, `Service.deliver` | `Service.campaign`, `Service.status`, `plan`, the report |
| `units` | `Service.create_workspace`, `create_campaign_workspace`, `integrate._mark_delivered`, `gc` (branch prune) | `Service.status`, `current_unit`, `unit_detail`, `gc`, `campaign.build_skeleton` |
| `candidates` | `Service._record_wave_commits`, `integrate._mark_delivered` | `Service.status`, `packet.build_packet`, `campaign.build_skeleton` |
| `checks` | `CheckRunner.run` | `Store.find_check/get_check/latest_check_for_commit/check_counts`, `packet._evidence_map` |
| `review_decisions` | `Service.review_decision`, `consume_approvals` | `Service.campaign_decision`, `packet.build_packet`, `require_all_approved` |

## 8. Status and value domains

| Table | Column | Values |
|---|---|---|
| `campaigns` | `state` | `working` (default), `delivered`, `closed` |
| `units` | `state` | `working` (default), `landed`; `closed` reserved |
| `units` | `kind` | `worker` (default), `campaign` |
| `candidates` | `status` | `prepared` (default), `pending`, `landed`, `failed`, `blocked` |
| `checks` | `status` | `passed`, `failed`, `error` |
| `checks` | `gpu` | `none`, `T1`, `T2` |
| `review_decisions` | `action` | `approve`, `request_changes`, `override` |

## 9. Concurrency and integrity

- `Store.tx()` commits on success and rolls back on exception. Methods that
  write outside it rely on the caller to `self.store.conn.commit()`; the check
  runner commits after it records a row.
- Foreign keys are on. There are no cascade rules, and Sliceme deletes no rows,
  so referential integrity holds by construction rather than by cleanup.
- WAL plus `busy_timeout` allows a coordinator, children, and a second terminal
  to read the same plane safely.
- The unique constraints that matter for correctness are `units.name` and
  `campaigns.key`, `campaigns.target_branch`, and `campaigns.worktree_branch`.
- The `checks` index on `fingerprint` makes the cache lookup cheap.

## 10. What is not in the database

- `dag.json` (the authored plan) is a file under `.sliceme/`. The human writes
  it, and the engine reads it. `state.json` is an optional, read-only legacy
  override that the engine never writes; git plus the database win on conflict.
- The suspend/resume descriptor `.sliceme/<branch-key>.session.json` is a file.
  `status --sessions` and `status --resume` read it directly; there is no
  `campaign_sessions` projection.
- Child events, per-child progress, and run control belong to pi-subagents.
  Sliceme keeps no `events.jsonl`, no per-node heartbeat, and no `jobs` table.
- Worker logs are files, one per node.
- The report is a Markdown file, included in the review packet.

## 11. Suspend/resume

The suspend/resume descriptor is the pi adapter's file. The engine reads it in
`status --resume` and `status --sessions` and keeps no database copy, so the
descriptor is the single source of truth. Resume reconciles from git plus
`state.db`; the `checks` table serves the cached verification, so a resumed node
does not re-run a full check.

## 12. Local review

`review_decisions` is an append-only decision log: the newest row for a campaign
wins, and `consumed_at` marks a delivery that already used the decision. One
approval covers the whole campaign commit set, so `Service.deliver` refuses
until a human approves the campaign. `gc` prunes rows for campaigns without a
descriptor older than the retention window (`policy.review_retention_days`,
default 30) and never prunes the current campaign. See `docs/review.md` for the
design.
