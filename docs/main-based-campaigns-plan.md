# Main-based campaigns plan

Status: proposed. Revised after an oracle review. This document describes a
change to the Sliceme delivery model. It records the agreed decisions and the
work plan.

## 1. Goal

The user wants a simpler campaign flow with one human gate. The new flow does
these steps:

1. Sliceme fetches the default branch (`main`).
2. Sliceme bases the campaign worktree on the fetched default branch.
3. Sliceme does not ask the user for a target feature branch.
4. The coordinator derives the campaign branch from the design file name.
5. The planner writes the plan, then Sliceme creates the worktree.
6. After the last wave, Sliceme collects the evidence and writes a summary.
7. The coordinator asks the user one time to open the pull request against
   `main`.

The user permission for the pull request is the only human gate.

## 2. Decisions

| Item | Decision |
|---|---|
| Pull request base | `main`, the default branch. |
| Campaign branch name | `feat/<name>`. |
| Feature name source | The coordinator derives it from the design file name. |
| Evidence narrative | Deterministic, written by the engine. No narrative agent. |
| `--target` parameter | Removed. It is not necessary. |
| Campaign identity | The campaign branch. |
| File key | `branch_key(campaign_branch)`. |

The engine still refuses to push the default branch. The campaign branch is the
push target. The pull request base is `main`.

## 3. Terms

| Term | Meaning |
|---|---|
| delivery base | The default branch. The pull request base. One value for each plane. |
| campaign branch | The feature branch. The pull request head. It is `feat/<name>`. |
| evidence | The commits, the checks, the diffs, and the logs of a campaign. |

`target_branch` keeps its name. It now means the **campaign branch**, the pull
request head. The engine adds `delivery_base` for the pull request base.
`worktree_branch` stays equal to `target_branch`.

The plan does not add a second identity column. The engine matches campaigns by
`target_branch` in `get_campaign`, `set_campaign_state`,
`set_campaign_pull_request`, and `update_campaign_target`. The pi adapter reads
`target_branch` from the `deliver` reply. A new `campaign_branch` column would
break these call sites.

## 4. Behaviour change

| Step | Before | After |
|---|---|---|
| `start` | Asks for a target feature branch. | Fetches `main`. Records the delivery base. |
| plan | Writes `dag.json` for the chosen target. | Writes `dag.json` for the campaign branch. |
| worktree | The target branch decides the name. | Sliceme creates it after the plan, from the campaign branch. |
| waves | No change. | No change. |
| end | A human approves the campaign. | The engine writes the evidence summary. |
| delivery | The base is a feature branch. | The base is `main`. The user permits the pull request one time. |

## 5. Corrections from the second opinion

The oracle review changed these points:

1. **One identity column.** Keep `target_branch` as the campaign branch. Add
   only `delivery_base`. Do not add `campaign_branch`.
2. **No plan key.** The coordinator knows the design file name before the
   planner runs. It fixes `feature_branch = feat/<design-slug>` first. The
   planner writes the same `dag.json` path as today. There is no second key
   space and no chicken-and-egg.
3. **The review diff base.** `packet._target_branch` must return
   `delivery_base`. Today it returns the campaign branch. When the campaign and
   the worktree branches become equal, the diff is empty and every report and
   evidence set is empty.
4. **No `open` host grant.** The campaign token rejects `feat/x`. The
   coordinator runs `sliceme wave --open` before the resource starts.
5. **Worker logs.** The code builds worker log paths but no writer exists.
   Confirm the writer, or capture the subagent output instead.
6. **Removed surface.** Remove the target question from the coordinator prompt,
   the `/sliceme` command message, and the tool parameters.
7. **The pull request body.** `pull_request_content` prints `Target branch:`.
   Change it to name the delivery base and the campaign branch.

## 6. Work plan

### Phase 1: identity and schema

Change these files:

- `sliceme/store.py`: add a nullable `delivery_base` column in `_migrate`.
  Backfill it from `found_default_branch` for existing rows.
- `sliceme/service.py`: keep the `config` mirrors. Add `delivery_base`.
  `target_branch` and `main_branch` mirror the campaign branch.
- `sliceme/integrate.py`: add `delivery_base_of(config)`.

Rules:

- `campaigns.target_branch` stays `TEXT NOT NULL UNIQUE`. It is the campaign
  branch.
- `worktree_branch` stays equal to `target_branch`.
- `_migrate` only adds columns. It cannot drop the old `UNIQUE` constraints.

### Phase 2: start without a target question

Change these files:

- `sliceme/verbs/bootstrap.py`: `init_plane` records the campaign branch and
  `delivery_base`. Remove `_resolve_target_branch`, `_retarget_plane`, and
  `_sync_campaign_retarget`.
- `sliceme/verbs/support.py`: remove `_resolve_target_branch`.
- `sliceme/verbs/sessions.py`: read the campaign branch from `target_branch`.
- `sliceme/surface.py`: remove the `target` and `target_mode` parameters from
  `start`.
- `integrations/pi/coordinator.ts`: remove `target` and `target_mode` from
  `ACTION_FLAGS`. Remove the target-branch gate from the prompt guidelines and
  the `/sliceme` command message.
- `integrations/pi/agents/planner.md`: require `feature_branch`. Set `base` to
  the delivery base.

The `start` action does these steps:

1. Fetch `main`.
2. Derive the campaign branch: `feat/<slug(design-stem)>`.
3. Record `delivery_base` and the campaign branch.
4. Create the campaign row.

For a design with a `sliceme-campaigns` block, each entry provides its own
`target`. The coordinator uses the entry target as the campaign branch.

### Phase 3: create the worktree from the fetched default branch

Change these files:

- `sliceme/verbs/campaign.py`: `create_campaign_workspace` fetches the delivery
  base and bases the worktree on `origin/<delivery_base>`.

The flow is:

```text
sliceme start DESIGN.md          # plane; fetch main; record the campaign branch
planner subagent                 # writes .sliceme/<branch-key>.dag.json
sliceme wave --open              # fetch main; create the worktree from origin/main
subagent(workflow: "sliceme.campaign")
```

Sliceme creates the worktree after the plan. It already matches "create the
worktree according to the feature name."

### Phase 4: delivery to main

Change these files:

- `sliceme/integrate.py`: `deliver_pull_request` refuses only when the campaign
  branch is the default branch. Remove the base refusal. Use
  `base=delivery_base`.
- `sliceme/review/packet.py`: `_target_branch` returns `delivery_base`.
- `sliceme/campaign.py`: `pull_request_content` names the delivery base and the
  campaign branch.
- `integrations/pi/coordinator.ts`: the `deliver` reply returns
  `feature_branch`.

Guards to keep:

- Keep `is_default_branch(root, campaign_branch)` in `init_plane` and
  `create_campaign_workspace`.
- Refuse `deliver` when the campaign branch equals `delivery_base`.
- Keep the delivery lock, the pre-push check run, and the permission row.
- Cache `delivery_base` at `start`, because `origin/HEAD` can change.

### Phase 5: evidence collection and summary

Change these files:

- `sliceme/review/packet.py`: bound the check output. Keep the full output in
  the JSON. Add the changed files and the diffstat for each commit.
- `sliceme/campaign.py`: extend `build_skeleton` and `render`.
- `sliceme/verbs/delivery.py`: add an `evidence` action.
- `sliceme/surface.py`: register the `evidence` action.
- `integrations/pi/coordinator.ts`: run the evidence action, then write the
  report.

The engine writes `.sliceme/<key>.evidence.json` and `.evidence.md`. The
document holds these items:

- the design reference and the campaign branch;
- every campaign commit, oldest first;
- the node, the goal, and the owned directories for each commit;
- the newest check for each commit, with the status, the command, the
  fingerprint, the duration, and the output;
- the diffstat and the changed files for each commit;
- the worker log paths and the tail of each log;
- the artifact paths;
- the report path.

The narrative is deterministic. The engine writes it. The document becomes the
pull request body.

### Phase 6: the single gate

Change these files:

- `integrations/pi/coordinator.ts`: add the confirm step.
- `sliceme/verbs/delivery.py`: keep `review_decisions` as the permission row.

The flow is:

```text
the resource returns complete
the coordinator writes the evidence summary and the report
the coordinator shows the summary and the pull request details to the user
the user confirms  -> review --decision approve -> deliver
the user declines  -> stop; keep the campaign for a later attempt
```

The gate shows the head branch, the base branch (`main`), the title, and the
evidence summary. The permission is the only pause. Suspension stays an
explicit user command, not a gate.

### Phase 7: documents and tests

Update these documents:

- `README.md`, `docs/guide.md`, `docs/workflow.md`, `docs/reference.md`
- `docs/review.md`, `docs/sessions.md`, `docs/multi-campaign.md`
- `docs/database.md`, `docs/architecture.md`, `docs/observability.md`
- `integrations/pi/agents/planner.md`

Update or add these tests:

- `tests/test_target_branch.py`: remove the target question. Assert that the
  delivery base is `main`.
- `tests/test_pull_request.py`: assert that the head is the campaign branch and
  the base is `main`.
- `tests/test_review_approval.py`: the gate is the permission.
- `tests/test_campaigns.py`, `tests/test_plan.py`, `tests/test_wave_scope.py`,
  `tests/test_status_summary.py`, `tests/test_cli.py`,
  `tests/test_pi_package.py`
- `tests/campaign_resource_test.mjs`, `tests/deliver_descriptor_test.mjs`
- `tools/fake_plane.py`
- New tests: `tests/test_evidence.py`, `tests/test_open_campaign.py`

Run `npm test` and `npm run typecheck`.

## 7. Phase order

Phases 1 and 2 are a hard prerequisite for 3 and 4. Phase 5 is independent and
can run in parallel. Phase 6 depends on 4 and 5. Phase 7 follows each phase.

## 8. Test plan

| Test | Purpose |
|---|---|
| `test_target_branch.py` | The delivery base is `main`. `--target` is gone. |
| `test_pull_request.py` | The head is `feat/<name>`. The base is `main`. |
| `test_open_campaign.py` | The worktree comes from `origin/main`. |
| `test_evidence.py` | The evidence document holds the commits and the checks. |
| `test_review_approval.py` | Delivery needs the permission row. |
| `test_e2e.py` | The full flow works from `start` to delivery. |

## 9. Risks

| Risk | Control |
|---|---|
| The identity change touches many files. | Keep `target_branch` as the campaign branch. Add only `delivery_base`. |
| The base `main` reverses a hard rule. | Push only the campaign branch. Keep the permission gate. |
| The fetch fails when the remote is absent. | Fall back to the local default branch. Report the fallback. |
| Two campaigns pick one feature branch. | Reuse `_unique_branch` for the campaign branch. |
| The evidence document is large. | Bound the output in the Markdown. Keep the JSON complete. |
| The worker log writer is absent. | Confirm the writer, or capture the subagent output. |

## 10. Out of scope

- A remote scheduler.
- A second human gate.
- A narrative agent for the evidence.
- Per-commit approval.

## 11. Removed items

- The `--target` parameter of `start` and `deliver`.
- The `--target-mode` parameter of `start`.
- The base refusal in `deliver_pull_request`.
- The target-branch question in the coordinator prompt and the `/sliceme`
  command message.
- `_resolve_target_branch`, `_retarget_plane`, and `_sync_campaign_retarget`.
