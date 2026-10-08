# Sliceme simplification plan

## Purpose

Sliceme gets smaller. This plan keeps two building blocks and one pi-subagents
workflow resource. The plan lists the code to delete, the interfaces to keep,
and the migration stages.

An oracle review of the first draft found four blockers. This revision folds in
every correction.

## Owner decisions

The owner made four decisions for this design.

1. The GPU single-runner serialization can be one bounded lane. Delete the
   serialized broker; keep the host runner.
2. Per-pull-request approval is acceptable. Keep one campaign approval.
3. Move the coordinator loop into a pi-subagents workflow resource.
4. Keep the suspend and resume behavior.

Decision 4 sets a hard constraint. The resume cache and the delivery evidence
must survive. This plan keeps a persistent check cache for that reason.

## Goals

- Publish two building blocks: one Python engine and one Pi extension.
- Delete the browser review subsystem.
- Delete the duplicate scheduler and the duplicate readiness code in TypeScript.
- Delete the persistent check queue, the worker lease, and the GPU broker's
  serialization.
- Let pi-subagents own child execution, tool scoping, status, and control.
- Keep the plan, the scheduler, the campaign worktree, and delivery in sliceme.
- Keep the suspend and resume behavior.

## Non-goals

- Do not port the scheduler to TypeScript.
- Do not build a second review client.
- Do not build a general workflow engine.

## Target architecture

```text
pi session
  |
  |  subagent({ workflow: "sliceme.campaign", cwd: <repo>, args, async: true })
  v
sliceme-pi extension (TypeScript)
  - registers the engine tool for interactive verbs
  - registers the agent definitions (planner, worker)
  - registers the trusted workflow resource "sliceme.campaign"
        |
        |  runs.host  ->  <absolute engine path> --json <verb>
        |  runs.run   ->  pi-subagents children
        v
sliceme-engine (Python)
  - scheduler: owns algebra, waves, readiness, GPU isolation
  - workspace: git worktree, plane files, per-node commits
  - checks: one synchronous combined-tree runner plus a check cache
  - delivery: approval gate, push, pull request
  - sessions: suspend and resume
```

## Building block A: sliceme-engine

The engine is one Python package with one verb surface. The verb surface stays
`surface.py` `ACTIONS` and `dispatch`. The Pi bridge calls this surface only.

The package keeps these internal modules.

| Module group | Files | Responsibility |
|---|---|---|
| scheduler | `sliceme/ownership.py`, `sliceme/plan.py` | owns algebra, wave packing, GPU isolation, readiness, design parse |
| workspace | `sliceme/gitutil.py`, `sliceme/campaign.py` | git worktrees, plane files, per-node commits |
| forge | `sliceme/pullrequest.py` | the `gh` client |
| checks | `sliceme/verifier.py`, `sliceme/sandbox.py`, `sliceme/checks.py` | one synchronous combined-tree check runner plus a check cache |
| delivery | `sliceme/integrate.py` | default-branch guard, checks, push, pull request |
| state | `sliceme/store.py` | SQLite state |
| verbs | `sliceme/service.py`, `sliceme/surface.py`, `sliceme/cli.py` | the verb facade and its adapters |
| sessions | `sliceme/campaign.py`, `sliceme/service.py` | suspend and resume |

`checks.py` is a new small module. It replaces the executor queue. It runs one
check set on a wave tree and returns the result. It does not hold a lease and it
does not queue work.

### Engine verbs after the change

Keep these verbs.

- `start`: create the plane and the campaign.
- `status`: return campaign state, node state, `ready`, `dag_waves`, and
  `paused`. Keep the `--sessions` and `--resume` flags.
- `ready`: return the ready node ids for the current wave, the current wave
  index, and `paused`.
- `plan`: parse the design and join the registry.
- `wave --record --current`: commit the finished nodes of the current wave. The
  `--current` flag is new. The engine reads the current wave index from its own
  state.
- `check --current`: run the synchronous combined-tree checks for the current
  wave. The `--current` flag is new.
- `review --decision`: record one campaign approval.
- `review --report`: render the deterministic report.
- `deliver`: push the campaign branch and open the pull request.
- `sessions`, `resume`: keep the session plane.

Delete these verbs.

- `exec` queue verbs: `--submit`, `--run`, `--validate`.
- `attempt --begin`, `attempt --end`.
- `progress`.
- `review --serve` and all comment verbs.

### Scheduler change for GPU serialization

The current scheduler packs waves by dependency, wave size, and directory
overlap. It ignores the `gpu` field. The loop would then start GPU nodes at the
same time.

The scheduler must treat a GPU node as a conflict with every other node. Then a
GPU node lands in its own wave. One GPU node runs at a time. This gives the one
bounded lane of owner decision 1 without a serialized broker.

### Persistent check cache

Owner decision 4 needs the cache. Resume re-verifies a node from the cache
instead of a full check. Delivery reads the check results as evidence.

Keep a `checks` table in `store.py`. One row holds the fingerprint, the wave,
the commit, the status, and the results. The synchronous runner reads a row by
fingerprint and writes a row after a check. The table replaces the old `jobs`
table. The table holds a cache, not a queue.

## Building block B: sliceme-pi

The extension stays thin. It registers three things.

1. One engine tool. The tool forwards verbs to the engine and returns JSON.
   Workers use it read-only. The coordinator uses it for interactive verbs.
2. Agent definitions through `pi-subagents/agents`. Keep `sliceme-planner` and
   `sliceme-worker`. Delete `sliceme-addressing`. Delete `sliceme-verifier`.
   The loop uses the builtin `reviewer` for wave review.
3. The trusted workflow resource `sliceme.campaign`.

The extension resolves the absolute engine path once in `session_start`. It
reuses `resolveSlicemeInvocation` from `integrations/pi/common.ts`. The resource
`resolve` function must do no I/O, so it reads the captured path from the
closure.

Delete the TypeScript orchestration code: `runSubagent`, `runTracked`,
`spawnNodes`, `runWorker`, `verifyNodes`, `readyNodes`, `normalizeDir`,
`ownsOverlap`, `ensureWaves`, `refreshWaves`, and `reconcileWaves`. Delete
`unit.ts` and its action mirror with the lockstep test.

Keep the session descriptor writer and the pause flag. The next section says
why.

## The campaign workflow resource

The resource name is `sliceme.campaign`. The extension registers it in
`session_start` and disposes it in `session_shutdown`.

### Input contract

The resource reads the plane from the workflow `cwd`. The caller sets `cwd` to
the repository root.

The `resolve` function accepts a small set of fields. Each field is a bounded
token or a boolean. The function rejects every other field.

| Field | Type | Rule |
|---|---|---|
| `campaign` | string, optional | `[A-Za-z0-9._-]{1,128}` |
| `waveCap` | integer, optional | 1 to 64 |
| `nodeCap` | integer, optional | 1 to 256 |

The `campaign` field is the only variable text in a command. The function
validates it against the strict pattern before it builds a command.

### Host authority constraint

A host grant binds an exact key and command pair. The `resolve` call cannot know
a node id or a wave index before the campaign runs. Therefore no granted command
carries a node id or a wave index. The engine reads the current wave and the
finished nodes from its own state.

The `resolve` function uses the absolute engine path. It never uses a relative
path. A relative path fails, because the resource runs with the workflow `cwd`
set to the target repository.

### Fixed host commands

Let `<engine>` be the absolute engine path captured in `session_start`. Let
`<campaign>` be the validated optional campaign token.

| Key | Command | Use |
|---|---|---|
| `status` | `<python> <engine> --json status --campaign <campaign>` | read campaign state, waves, and `paused` |
| `ready` | `<python> <engine> --json ready --campaign <campaign>` | get the ready node ids and the current wave |
| `record` | `<python> <engine> --json wave --record --current --campaign <campaign>` | commit the finished nodes |
| `check` | `<python> <engine> --json check --current --campaign <campaign>` | run the combined-tree checks |
| `approve` | `<python> <engine> --json review --decision approve --campaign <campaign>` | record the campaign approval |
| `deliver` | `<python> <engine> --json deliver --campaign <campaign>` | push and open the pull request |

The `resolve` function builds each command from fixed literals and the validated
campaign token. It does not concatenate task text or free-form flags.

## Campaign loop

The resource script runs this loop.

1. Call `ready`. Read the ready node ids, the current wave, and `paused`.
2. If `paused` is true, stop the loop and return the paused state.
3. If no node is ready, stop the loop.
4. Call `runs.run` for each ready node with a stable key and the worker agent.
5. Wait for the wave children with `runs.all`.
6. Call `record` to commit the finished nodes.
7. Call `check` for the wave evidence.
8. Call `runs.run` for one builtin `reviewer` child. Give the reviewer the
   recorded evidence from step 7.
9. Stop the loop when the wave count reaches `waveCap` or the node count reaches
   `nodeCap`.
10. Repeat from step 1.
11. Return the campaign summary.

The loop uses `runs.host` for engine state and `runs.run` for children. The
workflow sandbox has no file access, so all state arrives as JSON from the
engine.

The loop verifies after `record`, not before. A pi-subagents acceptance gate on
the worker runs before `record`, so a gate would test the mixed wave worktree.
The loop instead records the node commits first, then checks and reviews the
recorded evidence.

Step 8 uses the builtin `reviewer` child, so the loop does not need a sliceme
verifier agent. The reviewer reads evidence only. The reviewer never edits.

## Suspend and resume

Suspend has two parts: the pi-subagents run controls and the engine session
plane. Both stay.

The engine owns the pause flag and the session descriptor. The `ready` and
`status` responses carry `paused`. The loop reads `paused` through `runs.host`,
so the sandbox needs no file access.

The coordinator tool still launches the resource and writes the session
descriptor with the workflow run id. The pause verbs stay.

Resume works on two levels. First, pi-subagents reuses a finished child when the
same resource and the same args run again. Second, the engine rebuilds state
from git and SQLite. The `checks` table serves the cached verification, so a
resumed node does not re-run a full check.

## Verification after the change

Per-wave verification moves to a pi-subagents `reviewer` child after `record`.
Worker children carry no acceptance gate that depends on the wave.

The engine keeps one synchronous combined-tree check. Delivery and simulation
call this check. This check has no child, so a per-child gate cannot replace it.

The GPU broker's serialization goes away; the host runner stays. The scheduler
change above makes each GPU node a single-node wave.

## State changes

The `store.py` schema loses two tables and one column group.

| Table | Action |
|---|---|
| `campaigns` | keep |
| `units` | keep |
| `candidates` | keep |
| `checks` | add. Replaces `jobs` as a cache only |
| `jobs` | delete |
| `attempts` | delete |
| `comments` | delete |
| `review_decisions` | keep one approval row per campaign |

Keep `state.json`, `session.json`, and `control.json` (the campaign pause
flag). Drop `.dag_fingerprint`, because this plan deletes its writer. Delete
`events.jsonl` and `progress_<node>.json`. Pi-subagents owns child status,
events, and control.

Delete `require_comments_addressed` and `unaddressed_comments`. Remove the
delivery call at `service.py:2150-2152`. Otherwise delivery fails on a missing
table.

## Review subsystem change

Keep a reduced review module. Delete only the browser parts.

| File | Action |
|---|---|
| `sliceme/review/api.py` | keep the decision and report handlers |
| `sliceme/review/packet.py` | keep `campaign_branch_key` and the report packet |
| `sliceme/review/diff.py` | keep, because the packet uses it |
| `sliceme/review/server.py` | delete |
| `sliceme/review/security.py` | delete |
| `sliceme/review/web/*` | delete |
| comment handlers | delete |

Keep the approval gate and one `review --decision` verb. Keep `review --report`.

## Lock and status change

`_dispatch_wave` uses the executor lock today. Replace that lock with the
campaign lock, because checks are synchronous. The `status` response drops the
`executor` field and returns `checks` counts.

## Requirements relaxed

- Replace "one executor drains a check queue" with "one synchronous
  combined-tree check plus a check cache".
- Replace "a human approves each commit in the local review client" with "a
  human approves the campaign once before delivery".
- Drop browser review and per-line comment threads.
- Drop the addressing role and the comment lifecycle.
- Drop the multi-campaign plan block.

## Interfaces

The engine command line interface (CLI) is the only engine boundary. Every verb
accepts `--json` and returns JSON on standard output.

The resource is the only orchestration boundary. It hides the loop from the
session.

The agent definitions are the only child boundary. They carry the prompt, the
tool list, and the acceptance policy.

## Migration stages

Each stage keeps the tests green.

1. Add the explicit `ready` verb and the `paused` field. Read engine readiness
   in the bridge. Delete the TypeScript readiness code.
2. Delete the browser review server, the security module, the web client, and
   the comment verbs. Keep `review --decision`, `review --report`, and the
   approval gate. Rewrite the delivery guard.
3. Add `wave --record --current` and `check --current`. Add the `checks` table.
   Replace `executor.py` with `checks.py`. Delete the `jobs` and `attempts`
   tables.
4. Add GPU isolation to `ownership.py`. A GPU node conflicts with every node.
5. Register the agent definitions through `pi-subagents/agents`. Delete the
   `runSubagent` path.
6. Register the `sliceme.campaign` resource. Move the loop into the resource.
   Delete the coordinator orchestration functions. Keep the descriptor writer
   and the pause flag.
7. Delete the child observability files and the live widget.
8. Split `service.py` by verb group. Keep `surface.dispatch` as the facade.
9. Run the ASD Simplified Technical English check
   (`~/.agents/skills/asd-ste100/scripts/ste-check.py`). Run the full test suite.

## Test plan

- Keep the scheduler tests: `tests/test_scopes.py`, `tests/test_waves.py`,
  `tests/test_merge.py`, `tests/test_plan.py`.
- Add one GPU isolation test. Two GPU nodes must land in two waves.
- Keep the workspace tests: `tests/test_campaign.py`,
  `tests/test_pull_request.py`.
- Keep the session tests: `tests/test_sessions.py`.
- Replace the executor tests with `checks.py` cache tests. A resumed node must
  read the cache.
- Delete `tests/test_review.py`. Add one approval-gate test and one report test.
- Add a resource test. The test checks the fixed grants and the field rejection.
- Update `tests/test_pi_package.py`. Drop the action-mirror test.

## Risks and open items

- The resource holds six engine commands. Three commands change state. Keep the
  grant list small and reject every unexpected field.
- A plane can hold more than one campaign. The optional `campaign` token selects
  one. Test the multi-campaign path.
- The resource loop has a default bound. An omitted `waveCap` is 64 and an
  omitted `nodeCap` is 256, so the loop always ends even when the caller
  supplies no cap.
- Pi-subagents validates a literal agent name before launch. The extension must
  register the sliceme agent names for the request `cwd` before the first run.
- The engine state and the pi-subagents run state can disagree after a crash.
  The `status` and `ready` verbs rebuild from git and SQLite. Test this path.

## Resolved items

- `review --decision request_changes` still needs a note. `review_decision`
  raises `request_changes needs a note` (`sliceme/verbs/review.py`), so the
  comment cut left that path intact and no replacement is due.

## Review record

Two advisors reviewed the design in council mode.

- `oracle` (forked context): run `ed3e0110-b444-49d9-aea4-cc35fb09e9bb`, then
  cross-examination run `c762b1e5-28f7-4cc2-be52-696c7b474a2d`.
- `reviewer` (fresh context): run `38cb98b2-6ab5-4a27-b311-6db66ef50385`, then
  cross-examination run `582bcc8c-b831-40c8-b6ee-33f2c19fe74c`.

An oracle second opinion reviewed the first draft of this plan: run
`825205aa-032d-46a8-a4b9-5bc344b96053`, verdict `endorse-with-changes`. This
revision folds in the four blockers and the major findings.

The advisors agreed on the review cut, the TypeScript duplication cut, the
observability cut, and the spawn replacement. The advisors converged on one
Python scheduler. The owner decisions resolve the two remaining open items: the
GPU lane and the approval level.
