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
- [review.md](./review.md) — the review client, the server, and the approval gate.
- [sessions.md](./sessions.md) — suspend and resume.

## 1. Terms

### 1.1 Plane

A **plane** is one repository root that holds a `.sliceme/` directory. The plane
is the unit of isolation for state, locks, and the executor.

One plane holds these items:

- one SQLite database at `.sliceme/state.db`;
- one executor lock at `.sliceme/executor.lock`;
- one delivery lock at `.sliceme/review.lock`;
- the repository's default branch, trusted checks, and policy.

Several pi sessions can share one plane. The plane does not belong to one
session.

### 1.2 Campaign

A **campaign** is one piece of work with a target branch. A campaign owns:

- one target (feature) branch;
- one campaign worktree branch;
- one campaign worktree;
- one DAG file and one state file;
- one review queue and one delivery.

Two campaigns in one plane differ by target branch. Two campaigns cannot share
a target branch.

## 2. Goal

The engine must support several campaigns in one plane at the same time.

Each campaign must have its own target branch, worktree, DAG, waves, review
queue, and delivery. The campaigns must share one executor and one database.

### 2.1 Constraints

- Keep the single executor per plane. It serializes checks and protects the GPU.
- Keep the engine as the owner of state. Adapters stay thin.
- Keep backward compatibility. An old plane must work as a one-campaign plane.
- Keep the rule that a target branch is unique in one plane.

## 3. Where each data item lives

Use the database for state that many processes change at the same time and that
the code queries or joins. Use files for bootstrap data, for agent documents,
and for rebuildable caches.

This rule keeps the current design and moves one item into the database.

| Item | Location after this change | Reason |
|---|---|---|
| Plane config | `.sliceme/config.json` | Small bootstrap data. Plane discovery reads it. |
| Campaign registry | `state.db` table `campaigns` | Many processes create and read it. The code joins it with units and candidates. |
| DAG | `.sliceme/<branch-key>.dag.json` | The planner subagent writes it with the Write tool. Files are the agent contract. |
| Coordinator cache | `.sliceme/<branch-key>.state.json` | A rebuildable cache. Git and the database win on conflict. |
| Session descriptor | `.sliceme/<branch-key>.session.json` | The adapter writes it. One file per campaign. |
| Pause flag | `.sliceme/<branch-key>.control.json` | The adapter reads and writes it. |
| Heartbeats | `.sliceme/<branch-key>.progress_<node>.json` | A worker writes them often. Files avoid database contention. |
| Worker logs | `.sliceme/<branch-key>.worker_<id>.log` | Append-only text. |
| Audit log | `.sliceme/<branch-key>.events.jsonl` | Append-only text. |
| Units, candidates, jobs, attempts, review rows | `state.db` | Already in the database. |

The answer to the data question is therefore:

- Move the campaign registry into the database.
- Keep `config.json` as plane bootstrap data.
- Do not move the DAG, the cache, the descriptor, or the logs.

### 3.1 Why the campaign registry goes into the database

The registry is mutable and concurrent. Two sessions can create a campaign at
the same time. SQLite in write-ahead logging mode gives atomic writes, and the
store already uses a busy timeout.

The registry must join with `units` and `candidates`. A table gives that join.

### 3.2 Why `config.json` stays a file

`init_plane` writes `config.json` before the database exists. Plane discovery
uses `config.json` as the marker. The pi adapter reads the file without a
subprocess.

The file holds only plane data after this change. The campaign data moves to the
registry.

## 4. Current blockers

The engine and the adapter mix the plane and the campaign. These places must
change:

| Blocker | Location |
|---|---|
| One config holds one target and one worktree branch. | `sliceme/service.py` `Service.config`, `init_plane` |
| `start --target` retargets the shared config. | `Service._retarget_plane` |
| One campaign worktree with unit name `campaign`. | `Service.create_campaign_workspace`, `wave_unit` |
| `units.name` is unique. A second campaign reuses the first unit. | `sliceme/store.py` schema |
| Delivery lock and integration worktree are plane-wide. | `Service._delivery_lock`, `integrate.main_worktree` |
| `_mark_delivered` marks every prepared candidate landed. | `sliceme/integrate.py` |
| `normalize_dag` reads all candidates, not one campaign's. | `Service.normalize_dag` |
| The review server builds one `Service` per plane. | `sliceme/review/server.py` `service_for` |
| The review packet reads the single config target. | `sliceme/review/packet.py` `campaign_branch_key` |
| The adapter finds the campaign in the single config. | `integrations/pi/coordinator.ts` `configuredBranch` |

## 5. Design decisions

### 5.1 Campaign identity

Use the target branch as the campaign identity. The primary key is the existing
`branch_key(target)`.

The `branch_key` function replaces `/` with `--`. Two branch names can collide.
For example, `feat/x` and `feat--x` give the same key. Add a check at create
time. Two campaigns must not share a key.

### 5.2 Plane config and campaign config

Keep `config.json` for plane fields: `version`, `default_branch`, `checks`,
`policy`, and `created_at`.

Put campaign fields in the registry: `target_branch`, `worktree_branch`,
`base`, `unit_name`, `name`, `design`, and `state`.

`Service.config` returns the plane fields plus the bound campaign fields. Most
call sites that read `config["target_branch"]` then keep working.
`Service.plane_config` returns the plane fields only, for the few plane-scoped
readers. `Service.campaign_config` is an alias for `Service.config`.

### 5.3 Locks

- Keep `.sliceme/executor.lock` plane-wide. One executor serves all campaigns.
- Keep `.sliceme/review.lock` plane-wide as the delivery lock. Deliveries run
  one at a time. This is the smallest safe change.
- Add `.sliceme/campaigns.lock` for campaign creation. It stops two creators
  from picking the same worktree path or branch name.

### 5.4 Session binding

The adapter must know which campaign a pi session owns.

Add a per-session pointer file `.sliceme/active.<process-id>.campaign`. Write it
at campaign start or resume. Remove it at shutdown. This follows the
`review.<process-id>.url` pattern. The pointer is the primary source; the
engine's `config.json` mirror is the fallback for a resumed session.

## 6. Data model

Add the `campaigns` table to `sliceme/store.py`:

```sql
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,            -- branch_key(target_branch)
  target_branch TEXT NOT NULL UNIQUE,
  worktree_branch TEXT NOT NULL UNIQUE,
  base TEXT,
  unit_name TEXT NOT NULL UNIQUE,
  name TEXT,
  design TEXT,
  state TEXT NOT NULL DEFAULT 'working', -- working | delivered | closed
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_campaigns_state ON campaigns(state);
```

Add a `campaign TEXT` column to `units`, `candidates`, `jobs`, and `attempts`.
The `jobs` and `attempts` tables now need the campaign. Node ids and sources can
repeat across campaigns.

Keep `latest_job_for_commit` unchanged. A commit hash is unique across
campaigns.

### 6.1 Store methods

Add these methods:

- `create_campaign`, `get_campaign`, `require_campaign`, `list_campaigns`, and
  `set_campaign_state`.
- A `campaign` filter on `list_units`, `list_candidates`, `list_attempts`, and
  `job_counts`.

## 7. Engine changes

### 7.1 `sliceme/service.py`

- Add the argument `campaign` to `Service.__init__`. Add `self.campaign` and a
  `resolve_campaign` helper.
- Resolve a campaign in this order: the explicit reference, the only campaign,
  or an error that says to pass a campaign. Accept a key, a target branch, or a
  unit name.
- Read `self.config` in every campaign-scoped method. Use `self.plane_config` for
  plane-only readers.
- Change `create_campaign_workspace` to derive the unit name and the worktree
  path from the campaign. Use `campaign:<key>` and `worktrees/campaign-<key>`.
  Register the campaign under `campaigns.lock`.
- Change these methods to take or resolve a campaign: `wave_unit`,
  `record_wave`, `normalize_dag`, `_dag_waves`, `_project_unit`, `status`,
  `resume`, `simulation`, `executor`, `review_*`, `deliver`, `report`,
  `remove_campaign_artifacts`, and `_log_review_event`.
- Make `status` return a plane summary when the caller names no campaign. The
  summary holds `default_branch` and a list of campaigns. Each list item has a
  short status.
- Make `_prune_reviews` keep every key in the `campaigns` table.

### 7.2 `sliceme/integrate.py`

- Change `_mark_delivered` to take a unit name. Mark only that campaign's
  candidates.
- Pass `campaign_config` into `deliver` and `plan_waves`.
- Deliver one pull request per campaign. The plane delivery lock serializes
  deliveries.

### 7.3 `sliceme/campaign.py`

- Change `build_skeleton` and `write_report` to filter by campaign.
- Add the collision check to `branch_key`.

### 7.4 `sliceme/surface.py`

- Add a `campaign` parameter to `start`, `status`, `deliver`, `wave`, `review`,
  `attempt`, and `exec`.
- Make `start --target` create a campaign. Do not retarget the plane.
- Pass the campaign from each handler into the `Service` or the method.

### 7.5 `sliceme/cli.py`

- Resolve the campaign before the code builds the `Service`.
- Add `--campaign` to `review --serve`.

## 8. Review server changes

Change `sliceme/review/server.py` and `api.py`:

- Hold the planes and their campaigns in `_Server`.
- Add `service_for(plane_key, campaign_key)`. Return a campaign-bound `Service`.
- Make `discover_planes` also list the campaigns of each plane.
- Add the `campaign` parameter to `/api/state`, `/api/diff`, and `/api/file`.
- Add `--campaign` to pin the server to one campaign. The pi coordinator uses
  this flag. Without the flag, the client shows a campaign selector.
- Read `params["campaign"]` in `/api/action`.
- Put the plane and the campaign in the URL fragment. Keep the write token
  there.

Change the client in `sliceme/review/web/app.js`:

- Read the campaign list from `/api/state`.
- Add a campaign selector next to the plane selector.
- Keep the selection in `location.hash`.

## 9. pi adapter changes

Change `integrations/pi/common.ts` and `coordinator.ts`:

- Add `activeCampaignPath(cwd, pid)`.
- Add `activeCampaign(ctx)`. Resolve in this order: the pointer file, the
  session descriptor, the only campaign, or a user prompt.
- Replace every `configuredBranch(ctx.cwd)` call with `activeCampaign(ctx)`.
- Pass `--campaign <branch>` to every campaign-scoped engine call.
- Make `startCampaign` create a campaign and write the pointer file.
- Replace `existingPlane` with `existingCampaign`.
- Make `ensureReviewServer` run `review --serve --campaign <branch>`.
- Use the pointer file in `session_start` and `session_shutdown`.

## 10. Migration

- Add only. Use `Store._ensure_columns` for the new columns.
- On the first use of a plane, check `config.json` for `target_branch`. If no
  campaign row exists, create one. Use the recorded unit name `campaign` and the
  recorded worktree path.
- Keep the legacy `config.json` fields as a mirror for one release. Point them
  at the migrated campaign. Old readers keep working.
- Do not rename the `campaign` unit or the `worktrees/campaign` directory.

## 11. Tests

Add tests under `tests/`:

- `test_campaigns.py`: create two campaigns in one plane. Check that the target,
  worktree, unit, DAG path, and state path differ.
- Scoping: record a wave in each campaign. Check that `candidates`, `jobs`, and
  `attempts` rows do not mix.
- Delivery isolation: deliver one campaign. Check that the other campaign stays
  `prepared` and `working`.
- Cleanup: run `gc`. Check that it keeps the other campaign's rows and files.
- Concurrency: create two campaigns at once. Check that the lock gives distinct
  branches and worktrees.
- Migration: build an old plane. Check that it becomes a one-campaign plane.
- Review: serve two campaigns from one server. Check the snapshot, the
  approvals, and the delivery.
- Adapter: extend `tests/state_store_test.mjs` and add a pointer-file test.
- Keep the surface-parity test green. Update `tests/test_pi_package.py` for the
  new parameters.

## 12. Delivery phases

Keep the tree shippable after each phase.

1. Data model. Add the table, the columns, the store methods, and the migration.
   Change no behavior.
2. Service binding. Add `Service.campaign` and `campaign_config`. Convert the
   campaign-scoped methods. Keep single-campaign behavior the same.
3. Campaign creation. Change `start`. Add the campaign lock and the collision
   check.
4. Integration and review scope. Scope `_mark_delivered`, `normalize_dag`, the
   report, and pruning. Add the `--campaign` parameters.
5. Review server. Add several planes and several campaigns. Add the selector.
6. pi adapter. Add the pointer file. Pass `--campaign` everywhere. Pin the
   review server.
7. Documents and tests. Update the documents in section 14.

Phase 2 is the risky phase. It has many call sites. Do phase 1 and phase 2
first, then add a second campaign in a test. That test proves the model before
the adapter and the server change.

## 13. Risks

| Risk | Response |
|---|---|
| Shared integration worktree | Keep the plane delivery lock. Move to a per-campaign worktree only if parallel delivery is necessary. |
| Executor contention | Several campaigns share one executor. Use the priority field if one campaign must go first. |
| Branch-key collision | Check at create time. Reject the second campaign. |
| Adapter reads config directly | Use the pointer file and the descriptor. Keep a fallback for one release. |
| Larger review page | Use the pinned campaign for a coordinator. Use the selector for the CLI server. |
| Cleanup during a run | Use the campaign `state` field as the guard. |

## 14. Documents to update

- [guide.md](./guide.md): remove the non-goal about concurrent campaigns. Add
  the campaign concept.
- [reference.md](./reference.md): remove "One campaign per plane". Add the
  `campaigns` table and the new parameters.
- [architecture.md](./architecture.md): update the state layout and the entity
  diagram.
- [database.md](./database.md): add the `campaigns` table and the new columns.
- [review.md](./review.md): move "More than one concurrent campaign per plane"
  from out of scope to supported. Document the selector.
- [sessions.md](./sessions.md): change the session rule to one session per
  campaign.
- [observability.md](./observability.md): remove the note about concurrent
  campaigns.

## 15. Campaign plans

One design document can declare a campaign split. The engine parses the plan.
The coordinator runs the entries in order.

### 15.1 The plan block

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
| `target` | yes | The feature branch for the campaign. |
| `base` | no | The base ref. The default is the previous entry's target, or the plane base for the first entry. |
| `dirs` | no | The directory scope for the planner. |

### 15.2 Why a plan exists

The engine allows one owner per directory inside one campaign. A directory that
several phases touch forces those phases into one node or into separate
campaigns. A plan makes the second choice explicit. Each campaign has its own
DAG, worktree, and record, so a later campaign may own a directory the earlier
campaign owned.

### 15.3 The commands

- `sliceme plan --design DESIGN.md` prints the entries in order, the registry
  state of each, and the next entry to run.
- `sliceme start --design DESIGN.md --campaign <name>` starts one entry. The
  coordinator reads the entry's target, base, and directory scope.
- The planner receives the scope and plans every node inside it.
- After delivery, the coordinator reports the next entry.

### 15.4 The order

The coordinator runs the entries in plan order. Each entry's base defaults to
the previous entry's target, so the changes accumulate. Delivery needs human
approval, so the coordinator reports the next entry instead of starting it
without a command.

## 16. Related documents

- [guide.md](./guide.md) — the model, ownership, and orchestration.
- [architecture.md](./architecture.md) — the parts and the campaign lifecycle.
- [database.md](./database.md) — the SQLite schema and row lifecycle.
- [review.md](./review.md) — the review client, the server, and the gate.
- [sessions.md](./sessions.md) — suspend and resume.
