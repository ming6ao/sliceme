# Sliceme multi-campaign design

Status: implemented.

This document describes how Sliceme runs more than one campaign in the same
repository at the same time. An earlier engine allowed one campaign per
repository. This design removes that limit.

The normative documents stay in force:

- [guide.md](./guide.md) — the model, ownership, and orchestration.
- [reference.md](./reference.md) — actions, modules, and state layout.
- [architecture.md](./architecture.md) — the parts and the campaign lifecycle.
- [database.md](./database.md) — the SQLite schema and row lifecycle.
- [review.md](./review.md) — the approval gate and the report.
- [sessions.md](./sessions.md) — suspend and resume.

## 1. Terms

### 1.1 Plane

A **plane** is one repository root that holds a `.sliceme/` directory. The plane
is the unit of isolation for state and locks.

One plane holds these items:

- one SQLite database at `.sliceme/state.db`;
- one workspace lock at `.sliceme/workspace.lock`;
- the repository's default branch, trusted checks, and policy.

Several pi sessions can share one plane. The plane does not belong to one
session.

### 1.2 Campaign

A **campaign** is one piece of work with a work branch. A campaign owns:

- one work branch (`feat/<name>`), the pull request head;
- one base branch (the pull request base, `main`);
- one campaign worktree;
- one DAG file and one session descriptor;
- one confirmation gate and one delivery.

Two campaigns in one plane differ by work branch. Two campaigns cannot share
a work branch.

## 2. Goal

The engine must support several campaigns in one plane at the same time.

Each campaign must have its own work branch, worktree, DAG, phases, and
delivery gate. The campaigns must share one check runner and one database.

### 2.1 Constraints

- Keep one synchronous check runner per plane. The directory conflict rule and
  one-node phases protect a shared resource.
- Keep the engine as the owner of state. Adapters stay thin.
- Keep backward compatibility. An old plane must work as a one-campaign plane.
- Keep the rule that a work branch is unique in one plane.

## 3. Where each data item lives

Use the database for state that many processes change at the same time and that
the code queries or joins. Use files for bootstrap data, for agent documents,
and for rebuildable caches.

| Item | Location | Reason |
|---|---|---|
| Plane config | `.sliceme/config.json` | Small bootstrap data. Plane discovery reads it. |
| Campaign registry | `state.db` table `campaigns` | Many processes create and read it. The code joins it with records. |
| DAG | `.sliceme/<branch-key>.dag.json` | The planner subagent writes it with the Write tool. Files are the agent contract. |
| Session descriptor | `.sliceme/<branch-key>.session.json` | The adapter writes it; it carries the per-node status map. One file per campaign. |
| Pause flag | `.sliceme/<branch-key>.control.json` | The adapter reads and writes it. |
| Active pointer | `.sliceme/active.<pid>.campaign` | The adapter binds a session to one campaign. |
| Worker logs | `.sliceme/<branch-key>.worker_<id>.log` | Append-only text. |
| Units, records, verdicts, approval rows | `state.db` | Already in the database. |

The answer to the data question is therefore:

- Move the campaign registry into the database.
- Keep `config.json` as plane bootstrap data.
- Do not move the DAG, the cache, the descriptor, or the logs.

### 3.1 Why the campaign registry goes into the database

The registry is mutable and concurrent. Two sessions can create a campaign at
the same time. SQLite in write-ahead logging mode gives atomic writes, and the
store already uses a busy timeout.

The registry must join with `records`. A table gives that join.

### 3.2 Why `config.json` stays a file

`init_plane` writes `config.json` before the database exists. Plane discovery
uses `config.json` as the marker. The pi adapter reads the file without a
subprocess.

The file holds only plane data after this change. The campaign data moves to the
registry.

## 4. Campaign identity and config

### 4.1 Campaign identity

Use the work branch as the campaign identity. The primary key is the
existing `branch_key(work_branch)`.

The `branch_key` function replaces `/` with `--`. Two branch names can collide.
For example, `feat/x` and `feat--x` give the same key. Sliceme checks this at
create time. Two campaigns must not share a key.

### 4.2 Plane config and campaign config

`config.json` keeps the plane fields: `version`, `default_branch`, `checks`,
`policy`, and `created_at`.

The registry keeps the campaign fields: `work_branch` (the pull request head),
`base_branch` (the pull request base), `unit_name`, `name`, `design`, and
`state`.

`Service.config` returns the plane fields plus the bound campaign fields. Most
call sites that read `config["work_branch"]` then keep working.
`Service.plane_config` returns the plane fields only, for the few plane-scoped
readers. `Service.campaign_config` is an alias for `Service.config`.

### 4.3 Lock

- One `.sliceme/workspace.lock` serializes every git mutation: campaign
  creation, phase recording, and delivery. It stops two creators from picking the
  same worktree path or branch name, and it serializes the git mutation of a
  record and a delivery.

Checks are synchronous and run inside the caller, so they need no lock of their
own.

### 4.4 Session binding

The adapter knows which campaign a pi session owns.

The adapter writes a per-session pointer file
`.sliceme/active.<process-id>.campaign` at campaign start or resume, and removes
it at shutdown. The pointer is the primary source; the engine's `config.json`
mirror is the fallback for a resumed session.

## 5. Data model

The `campaigns` table in `sliceme/store.py`:

```sql
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,            -- branch_key(work_branch)
  work_branch TEXT NOT NULL UNIQUE,   -- the work branch (pull request head)
  worktree TEXT,                        -- the campaign's one worktree path
  base_branch TEXT,                   -- the pull request base (default branch)
  unit_name TEXT NOT NULL UNIQUE,
  name TEXT,
  design TEXT,
  state TEXT NOT NULL DEFAULT 'working', -- working | delivered | landed | closed
  delivery_mode TEXT,                    -- pull_request | local
  merge_commit TEXT,                     -- the local merge commit
  pr_url TEXT,
  pr_number INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_campaigns_state ON campaigns(state);
```

The `campaign TEXT` column also appears on `records`. Node ids
and sources can repeat across campaigns, so the verdict and approval rows carry the
campaign or the branch key.

`latest_verdict_for_commit` reads the newest terminal check for a commit. A commit
hash is unique across campaigns.

### 5.1 Store methods

- `create_campaign`, `get_campaign`, `require_campaign`, `list_campaigns`, and
  `set_campaign_state`.
- A `campaign` filter on `list_records`, `list_verdicts`, and
  `verdict_counts`.

## 6. Engine behavior

### 6.1 `sliceme/service.py` and `sliceme/verbs/`

- `Service.__init__` takes the argument `campaign`. `Service.campaign` resolves
  it in this order: the explicit reference, the only campaign, or an error that
  says to pass a campaign. The reference accepts a key, a work branch, or a
  unit name.
- Every campaign-scoped method reads `self.config`. Plane-only readers use
  `self.plane_config`.
- `create_campaign_workspace` derives the worktree path from the campaign,
  under `workspace.lock`.
- `phase_unit`, `record_phase`, `normalize_dag`, `_dag_phases`, `_project_campaign`,
  `status`, `resume`, `simulation`, `check_phase`, the review verbs, `deliver`,
  `report`, and `remove_campaign_artifacts` take or resolve a campaign.
- `status` returns a plane summary when the caller names no campaign. The
  summary holds `default_branch` and a list of campaigns.
- `_prune_approvals` keeps every key in the `campaigns` table.

### 6.2 `sliceme/integrate.py`

- `_mark_delivered` takes a campaign and marks only that campaign's records.
- `deliver` and `plan_phases` receive the campaign config.
- One delivery per campaign, in one of two modes. The workspace lock
  serializes deliveries.

### 6.3 `sliceme/campaign.py`

- `build_skeleton` and `write_report` filter by campaign.
- `branch_key` includes the collision check.

### 6.4 `sliceme/surface.py` and `sliceme/cli.py`

- `start`, `status`, `ready`, `plan`, `deliver`, `check`, `open`, `record`,
  `approve`, `report`, and `evidence` all accept `campaign`.
- `start` creates a campaign for the derived (or `--feature-branch`) campaign
  branch. It does not retarget the plane.
- The CLI resolves the campaign before the code builds the `Service`.

## 7. Review surface

The reduced review surface is campaign-scoped:

- `packet.campaign_branch_key` reads the bound campaign.
- `packet.build_packet` returns the commits, the report, and the evidence of one
  campaign.
- `approve` records one campaign-level decision.
- `report` writes `.sliceme/<branch-key>.report.md` for one campaign.

A plane with several campaigns and no `--campaign` returns the plane summary
instead of one review.

## 8. pi adapter

`integrations/pi/common.ts` and `coordinator.ts`:

- `activeCampaignPath(cwd, pid)`, `readActiveCampaign`, `writeActiveCampaign`,
  and `clearActiveCampaign` own the pointer file.
- The `sliceme` tool writes the pointer when a reply carries a work branch,
  and it passes `--campaign <branch>` to every campaign-scoped engine call.
- The `sliceme.campaign` resource accepts the optional `campaign` field, so one
  plane can run one named campaign.
- `session_start` and `session_shutdown` read and clear the pointer.

## 9. Migration

- Add only. `Store._ensure_columns` adds the new columns.
- On the first use of a plane, check `config.json` for `work_branch`. If no
  campaign row exists, create one. Use the recorded writer name `campaign` and
  the recorded worktree path.
- Keep the legacy `config.json` fields as a mirror for one release. Point them
  at the migrated campaign. Old readers keep working.
- Do not rename the `campaign` writer name or the `worktrees/campaign` directory.

## 10. Tests

Tests under `tests/`:

- `test_campaigns.py`: two campaigns in one plane. The work branch,
  worktree, writer name, and DAG path differ.
- Scoping: record a phase in each campaign. The `records` and `verdicts` rows do
  not mix.
- Delivery isolation: deliver one campaign. The other campaign stays `recorded`
  and `working`.
- Cleanup: run `gc`. It keeps the other campaign's rows and files.
- Concurrency: create two campaigns at once. The lock gives distinct branches
  and worktrees.
- Migration: build an old plane. It becomes a one-campaign plane.
- Review: the packet and the approvals stay scoped to one campaign.
- Adapter: `tests/active_campaign_test.mjs` covers the pointer file.
- `tests/test_pi_package.py` keeps the package contract green.

## 11. Risks

| Risk | Response |
|---|---|
| Shared delivery worktree | Keep the workspace lock. Move to a per-campaign worktree only if parallel delivery is necessary. |
| Branch-key collision | Check at create time. Reject the second campaign. |
| Adapter reads config directly | Use the pointer file and the descriptor. Keep a fallback for one release. |
| Cleanup during a run | Use the campaign `state` field as the guard. |

## 12. Campaign plans

One design document can declare a campaign split. The engine parses the plan.
The coordinator runs the entries in order.

### 12.1 The plan block

A design document holds a fenced block:

```sliceme-campaigns
[
  {"name": "core", "target": "agent/post-training-core", "base": "main",
   "dirs": ["include/nanochat", "backends", "src", "bindings", "python", "tools"]},
  {"name": "sft", "target": "agent/post-training-sft",
   "base": "agent/post-training-core",
   "dirs": ["python", "tools", "tests", "src"]},
  {"name": "rl", "target": "agent/post-training",
   "base": "agent/post-training-sft",
   "dirs": ["src", "tools", "tests", "python"]}
]
```

Each entry has these fields:

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | The campaign name. Unique in the plan. |
| `target` | yes | The work branch for the campaign. |
| `base` | no | Deprecated and ignored. The base branch is the plane base branch (`main`). |
| `dirs` | no | The directory scope for the planner. |

### 12.2 Why a plan exists

The engine allows one owner per directory inside one campaign. A directory that
several phases touch forces those phases into one node or into separate
campaigns. A plan makes the second choice explicit. Each campaign has its own
DAG, worktree, and record, so a later campaign may own a directory the earlier
campaign owned.

### 12.3 The commands

- `sliceme plan --design DESIGN.md` prints the entries in order, the registry
  state of each, and the next entry to run.
- `sliceme start --feature-branch <branch> --campaign <name>` starts
  one entry. The coordinator reads the entry's target (the work branch) and
  directory scope.
- The planner receives the scope and plans every node inside it.
- After delivery, the coordinator reports the next entry.

### 12.4 The order

The coordinator runs the entries in plan order. Each entry bases its campaign
on the plane base branch (`main`). A later campaign sees an earlier
campaign's files only after those files merge into the base branch. Delivery
needs user confirmation, so the coordinator reports the next entry instead of
starting it without a command.

## 13. Related documents

- [guide.md](./guide.md) — the model, ownership, and orchestration.
- [architecture.md](./architecture.md) — the parts and the campaign lifecycle.
- [database.md](./database.md) — the SQLite schema and row lifecycle.
- [review.md](./review.md) — the approval gate and the report.
- [sessions.md](./sessions.md) — suspend and resume.
