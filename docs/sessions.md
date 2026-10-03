# Sliceme session suspend and resume design

Status: implemented. Phases 1–4 landed (engine `status --resume`/`--sessions`
and `attempt`, the adapter descriptor and pause flag, the `/suspend` and
`/campaigns` commands, and wave-scoped continuation); budget auto-suspend and a
paused-worktree prune policy remain future work. The descriptor file is the
only registry: there is no `campaign_sessions` table. Revised to
match the single-campaign-worktree refactor (commit `234389e`); the earlier
draft still assumed one unit worktree per node attempt, which the engine no
longer does.

This document describes how a pi agent session can be suspended when the user
asks and resumed later, and how the progress inside a long-running Sliceme
campaign is checkpointed so that resuming continues rather than restarts. It is
grounded in the current engine and adapter.

- Pi already stores every session as JSONL and reopens it with
  `pi --continue`, `pi --resume`, and `/resume` (`docs/sessions.md` in the pi
  distribution). The transcript needs no new storage.
- `sliceme/campaign.py` already prefixes DAG/state/report/log paths by the
  target (feature) branch (`branch_key`). The pi coordinator writes `dag.json`
  and `state.json`; Python reads them.
- `integrations/pi/coordinator.ts::startCampaign` already reconciles a campaign
  from git plus `state.db` on an existing `dag.json` (`guide.md` §Failure and
  resume): a `landed` candidate becomes `done`, and a node left `running` or
  `recorded` is reset to `pending` with its attempt count advanced.
- `sliceme/store.py::recover_orphan_jobs` returns expired `running` executor
  jobs to `queued`; `sliceme/executor.py::recover_orphans` calls it before a
  drain or wait. Git and `state.db` win over `state.json`.
- Workers are **pure editors in one shared campaign worktree**
  (`Service.create_campaign_workspace`, unit name `campaign`), which is never
  recreated or rebased between waves. A wave's changes are committed by
  `wave --record --wave N`, one commit and one `prepared` candidate per node.
- `docs/observability.md` already proposes the durable `attempts` table and the
  per-node heartbeat files this design uses for continuation fidelity.

The design adds an explicit resume descriptor, a discovery registry, a
cooperative stop, and wave-scoped continuation on top of that substrate.

## 1. Goals

- A user can suspend a pi session on request, at a safe point, without losing
  committed or uncommitted campaign work.
- A suspended session is discoverable, nameable, and resumable from the same
  checkout, from a second terminal, or after a process crash.
- A long Sliceme campaign resumes from the wave it stopped at: done nodes never
  re-run, committed candidates re-verify from cache, and a node whose worker was
  interrupted continues from the preserved campaign worktree.
- The user has a way to store and manage suspended sessions as campaigns.

### Non-goals

- Replacing pi's session picker. `/resume` stays pi's; this design adds campaign
  awareness on top of it.
- A daemon. Suspension is a file-and-SQLite checkpoint plus pi's own session
  file, consistent with "no long-lived daemon" (reference.md §6).
- Multiple concurrent campaigns per plane (still out of scope). One campaign per
  (plane, target branch).
- Remote or cross-repository scheduling.

## 2. Two layers, one lifecycle

There are two durable things, and one lifecycle that parks and wakes both.

```text
                 /suspend                 pi --continue / /campaigns
   active ───────────────▶ checkpointing ─────────▶ suspended
     ▲   (pause flag +      (adapter checkpoint)        │
     │    waitForIdle)                                  │
     └───────────── resume (switchSession + auto-prompt)┘
```

- **Pi session layer.** The transcript is autosaved JSONL. Suspending means
  reaching a safe point, parking, and registering; resuming means reopening the
  transcript and injecting a continuation prompt.
- **Sliceme campaign layer.** `dag.json`, `state.json`, `state.db`, and git
  already describe campaign progress. The resume descriptor adds only what they
  cannot: which pi session to reopen, why the pause happened, and which wave to
  record on resume.

The registry is the management surface. The resume descriptor is the
campaign-specific record. Pi's JSONL remains the transcript.

## 3. Pi session layer

### 3.1 Commands

The Sliceme extension registers one entry point per user intent. Commands keep
the adapter thin and forward to the engine where reconciliation is involved.

| Command | Behavior |
|---|---|
| `/sliceme [DESIGN.md]` | Existing start entry point. Activates the tools and starts a campaign. Refuses while the agent is busy (`ctx.isIdle()`). |
| `/suspend [label]` | Park the current session: write the pause control flag, abort the in-flight turn so the current worker and any engine subprocess exit, wait for idle, write the resume descriptor. An interrupted node is marked `paused` so resume continues its edits. |
| `/campaigns` | Interactive list of registered campaigns: label, target branch, wave, done/total, last activity, status. Actions: resume, rename, show, prune, delete. |

`/resume` is deliberately not registered: pi owns it, and `session_start` with
reason `"resume"` provides the campaign hook. `ctx.hasUI` guards interactive
dialogs; JSON and print modes fall back to text and the CLI.

`/suspend` runs in an `ExtensionCommandContext`, which adds `waitForIdle()`,
`switchSession()`, `newSession()`, `fork()`, and `reload()` on top of the base
`ExtensionContext` (`shutdown()`, `abort()`, `signal`, `isIdle()`, `hasUI`).
`/campaigns` uses `switchSession()` to reopen a saved session.

### 3.2 Event hooks

The pi extension API provides these events; the adapter does not register them
yet.

- `session_shutdown` (reason `"quit" | "reload" | "new" | "resume" | "fork"`)
  writes the resume descriptor synchronously, so any exit, not only an explicit
  `/suspend`, is resumable. The handler must stay fast and idempotent: use
  synchronous file writes and no subprocess calls. It must also tolerate
  `"reload"`, which fires when extensions reload.
- `session_start` (reason `"startup" | "reload" | "new" | "resume" | "fork"`)
  detects that the working directory is a suspended Sliceme plane with an open
  campaign for the current branch. Auto-inject the resume prompt only for
  `"resume"`; for `"startup"` offer it, never inject it, because a plain `pi`
  launch in a campaign checkout should not silently take over the turn. Before
  the prompt is sent or offered, re-activate the campaign tools: pi does not
  restore the active set from the transcript when a session is resumed, so the
  `sliceme` tool (registered `defaultActive: false`) would otherwise not be
  declared and the injected prompt would name an uncallable tool. The prompt
  itself reports the progress from the engine's resume plan — the open wave, the
  wave count, each wave's members and node statuses, and the pending
  record/continue/re-verify/respawn work — so neither the user nor the model has
  to run `status` first.
- `session_before_switch` (cancellable, reason `"new" | "resume"`) is an
  optional guard: write or verify the descriptor before allowing a switch.
- `agent_settled` (final, notification-only) is an optional turn-boundary
  checkpoint trigger, more reliable than inferring turn end.

`pi.appendEntry("sliceme.session", descriptor)` records a compact version in the
transcript so a reloaded session describes its own campaign state. Entries
appended here are not sent to the model.

### 3.3 Storage and management

The adapter is the single writer of the resume descriptor, matching the
existing rule that the orchestrator writes `dag.json` and `state.json`. The
engine reads the descriptor directly; it keeps no database copy.

| Store | Path | Owner | Role |
|---|---|---|---|
| Resume descriptor | `.sliceme/<branch-key>.session.json` | pi adapter | pi session binding, pause reason, wave, resume plan. |
| Global index | optional, see below | pi adapter | Cross-repository discovery cache; rebuildable. |
| Transcript | pi session JSONL | pi | Conversation history. |

Suspension is a pi-session concern, so there is deliberately **no engine
`suspend` action**: two rich writers in two languages for one file is the
failure mode this rule avoids. A second terminal inspects campaigns with
`sliceme status --sessions` and resumes by starting pi.

The optional global index reuses `integrations/pi/common.ts::agentDir()`. That
helper currently reads `PI_AGENT_DIR`; pi's documented configuration variable is
`PI_CODING_AGENT_DIR` (in the pi distribution's `docs/environment-variables.md`),
so the helper and this path
should be aligned. Pi session storage is a separate location, overridable by
`PI_CODING_AGENT_SESSION_DIR`, `--session-dir`, or the `sessionDir` setting, so
never assume `~/.pi/agent/sessions/--repo--/`.

To keep the global index race-free, discovery should be per-campaign fragment
files plus `cwd`-based lookup rather than one shared JSON document that several
planes read-modify-write. A `sliceme status --sessions` can regenerate it by
scanning descriptor files and pi session headers. For v1 it is simplest to drop
the global index entirely and discover campaigns from `ctx.cwd`.

## 4. Sliceme campaign layer

### 4.1 Engine actions

Add these to `sliceme/surface.py` (the source of truth). `sliceme/cli.py` is
generated from the registry; `integrations/pi/unit.ts::SLICEME_ACTIONS` and
`tests/test_pi_package.py` follow.

```text
sliceme status --resume  [--plan-only]
sliceme status --sessions
```

Both flags also print JSON under the CLI's global `--json` flag, which is how
the pi adapter calls the engine.

- `status --resume` reads `.sliceme/<branch-key>.session.json`, reconciles it
  with git plus `state.db`, and returns the resume plan. `--plan-only` reports
  without side effects.
- `status --sessions` is the engine view of the registered campaigns for a
  second terminal. It reads the descriptor files.

There is no engine `suspend`; the adapter writes the descriptor.

### 4.2 Resume descriptor

`.sliceme/<branch-key>.session.json`:

```jsonc
{
  "campaign": "nanochat-cpp",
  "feature_branch": "feat/nanochat-cpp",
  "worktree_branch": "sliceme/feat-nanochat-cpp",
  "design": "DESIGN.md",
  "pi": {
    "session_id": "uuid",
    "session_file": "/home/u/.pi/agent/sessions/--repo--/<id>.jsonl",
    "cwd": "/repo"
  },
  "label": "nanochat-cpp",
  "status": "suspended",           // active | suspended | ready | completed
  "reason": "user",                // user | crash | budget | error
  "suspended_at": 1733234400.0,
  "current_wave": 1,
  "waves": [
    { "index": 0, "members": ["w1", "w2"], "status": "done" }
  ],
  "nodes": {
    "w3": {
      "status": "paused",
      "attempt": 2,
      "candidate": 41,
      "commit": "abc123def456",
      "last_heartbeat": ".sliceme/feat--nanochat-cpp.progress_w3.json"
    }
  },
  "resume_plan": {
    "record_wave": 1,       // run wave --record --wave 1 first
    "resume": ["w3"],       // edits already in the campaign worktree
    "respawn": ["w4"],      // no attributable changes
    "verify": ["w3"],       // re-verify from cache after recording
    "blocked": []
  }
}
```

The descriptor deliberately does **not** duplicate `state.json`'s per-node
status; it records the node's status at suspend time plus the recorded commit,
so resume can detect drift. There is no per-node worktree or branch: the one
campaign worktree is shared by every node in a wave.

The descriptor status is terminal once the campaign is finished.  A successful
`deliver` writes `completed` immediately, not only from `session_shutdown`, so a
crash after the merge cannot leave a `suspended` descriptor behind.  When every
wave is done but delivery has not happened, `session_shutdown` writes `ready`
rather than `suspended`, because there is no wave left to continue.  As a
backstop, `session_start` re-derives the plan from the engine
(`resume --plan-only`) before offering or injecting a resume; if the plane shows
no remaining work it rewrites the descriptor to `completed` (delivered) or
`ready` (undelivered) and stays silent.  This keeps a stale `suspended`
descriptor — from a crash, a CLI delivery, or a hand-edited plane — from
re-offering a finished campaign on every launch.

### 4.3 Node states and reconciliation

Extend the current `pending | running | recorded | done | failed` (the
`NodeState` union also declares `stopped`, which nothing writes) with `paused`.
On resume, reconcile from plane evidence, which wins over the descriptor:

| Plane evidence | Resume status |
|---|---|
| latest candidate `landed` | `done`; never re-run |
| node `done` and recorded commit equals the latest candidate `head_commit` | `done`; re-verify from cache only |
| candidate `prepared` with a recorded commit | `recorded`; verify |
| candidate `prepared` but no attributable commit for the node | `pending`; re-spawn |
| node `running` or `paused` and the campaign worktree is present | `paused`; record, then continue |
| node `running` or `paused` but the campaign worktree is missing | `pending`; fresh spawn |
| executor job `running` past its lease | requeued by `recover_orphan_jobs` |

The commit comparison is essential. A node that was verified but not yet
delivered is `done` in `state.json` while its candidate is still `prepared`
(verification is a separate table and never changes the candidate row), so a
status-only rule would wrongly re-run it. This also matches what
`startCampaign` already does today.

### 4.4 Wave-scoped continuation

This is the main saving for long waves. The campaign worktree is never reset on
suspend. A `paused` node is one whose worker was interrupted before `record`, so
on resume the coordinator:

1. runs `wave --record --wave N` under the executor lock, committing whatever
   edits exist and attributing every changed path to exactly one node by its
   owned directories (`Service._record_wave_commits` diffs against `HEAD`, so
   earlier waves are not re-attributed);
2. re-verifies any node whose prepared candidate fingerprint is unchanged — the
   executor returns `cached`, so no check re-runs;
3. re-spawns any node that produced no attributable changes.

A continuation worker is spawned in the same campaign worktree with a prompt
built from:

- the node goal and acceptance from `dag.json`;
- `git status --porcelain` and `git diff --stat` in the campaign worktree;
- the last heartbeat (`last_tool`, a short argument, the last assistant text)
  from `.sliceme/<branch-key>.progress_<node>.json`;
- the recorded attempt number and the previous verifier evidence, if any.

A fresh spawn is the fallback when the worktree is gone or the diff cannot be
attributed.

### 4.5 Wave scope

The single campaign worktree holds several nodes' edits, so the recorder is
serialized with check runs by the executor lock. Suspend preserves the worktree
and records `record_wave` in the resume plan; resume records first (step 1
above) before verifying. Because `_record_wave_commits` diffs against the
current `HEAD`, re-recording a partly finished wave cannot re-attribute an
earlier wave's committed changes.

## 5. Cooperative suspension

A campaign is many tool calls in one long agent turn, so suspension must stop at
a node or wave boundary rather than mid-write.

1. `/suspend` writes `.sliceme/<branch-key>.control.json`:

   ```json
   { "pause": true, "requested_at": 1733234400.0, "label": "nanochat-cpp" }
   ```

   and sends a steering message (`pi.sendUserMessage(..., { deliverAs: "steer" })`)
   telling the coordinator to finish the current node and stop. Because the
   handler runs in an `ExtensionCommandContext`, it then `await ctx.waitForIdle()`
   before writing the descriptor, so the checkpoint always reflects a settled
   turn.

2. `spawnNode`, `ready`, `ensureWaves`, `recordWave`, and `verifyNode` check the
   flag and return a `paused` result instead of doing work. Tool guidelines tell
   the model to stop on `paused`. Covering `record` and `verify` matters: a pause
   issued while a wave is being recorded or verified must not be ignored.

3. When the turn settles, the descriptor is written, the campaign list is
   refreshed on the next engine call, and the user is told how to resume.

4. Resume clears the pause flag before doing anything else, and ignores a flag
   whose `requested_at` is older than a short time-to-live. Otherwise a crash
   while paused leaves `pause: true` behind and the resumed campaign parks again
   forever.

5. A hard abort (Escape, Ctrl+C) still works: `ctx.signal` propagates through
   `runSubagent` to a SIGTERM of the child (then SIGKILL after five seconds),
   and `session_shutdown` writes the descriptor. `SIGKILL` falls back to the
   existing recovery path.

6. If the model keeps issuing work after `paused`, escalate: a second `/suspend`
   or a timeout calls `ctx.abort()` so the turn cannot run unbounded.

A `--reason budget` suspend may be issued automatically when a wall-clock or
cost cap is hit, using the metrics from `docs/observability.md`.

## 6. Dependency: attempt fidelity

The quality of continuation depends on the durable half of
`docs/observability.md` (its Phase 1): the `attempts` table and the per-node
heartbeat files (`.sliceme/<branch-key>.progress_<node>.json`). They supply the
timestamps, `last_tool`, and per-attempt status the continuation prompt
consumes. `integrations/pi/common.ts::runSubagent` already streams each
subagent's `pi --mode json` output to a per-node log, so the accumulator and
heartbeat writer are a small addition. Build this substrate before, or together
with, the continuation work.

Note that resume **correctness** does not depend on `attempts`: git plus
`state.db` already reconcile a campaign. Only the §4.4 continuation prompt does.

## 7. Interfaces touched

| Layer | Files |
|---|---|
| Engine | `sliceme/surface.py` (`status --resume`, `status --sessions`), `sliceme/service.py` (reconcile + plan), `sliceme/campaign.py` (descriptor reader and session paths), `sliceme/cli.py` (generated) |
| Adapter | `integrations/pi/coordinator.ts` (tool verbs, pause flag, event hooks, resume prompt, `/suspend`, `/campaigns`), `integrations/pi/common.ts` (descriptor writer, registry IO, heartbeat), `integrations/pi/unit.ts` (`SLICEME_ACTIONS`) |
| Docs | this file; updates to `reference.md` (§1 actions, §3 state layout), `workflow.md`, `guide.md` (§Failure and resume), `database.md` (§11 related proposed work) |

This version touches **no** `integrations/pi/coordinator.ts` per-node worktree
code, because there is none.

## 8. Phased delivery

1. **Engine resume primitive.** The descriptor reader, `status --resume` /
   `--sessions`, and path helpers in
   `campaign.py`. No adapter change; testable from the CLI with a hand-written
   descriptor and reusing existing recovery.
2. **Attempt fidelity.** The `attempts` table, heartbeat files, and
   `attempt --begin`/`--end` wired into `spawnNode` and `verifyNode`, following
   `docs/observability.md`.
3. **Adapter checkpoint and pause.** `session_shutdown` descriptor write,
   `session_start` resume injection, the pause flag in `spawn`/`ready`/
   `record`/`verify`, and `/suspend` using `waitForIdle()`.
4. **Management UX.** `/campaigns`, `sliceme status --sessions`, `switchSession` resume,
   discovery, and `--rebuild`.
5. **Continuation and budgets.** Wave-scoped record-on-resume, continuation
   workers, budget auto-suspend, and a prune policy for preserved campaign
   worktrees.

## 9. Test plan

- `tests/test_sessions.py` (new): descriptor round-trip; `resume` maps `landed`
  to `done`, a `done` node with a matching commit stays `done`, a `prepared`
  candidate without a commit to `pending`, and a preserved worktree to
  `paused`; orphan job recovery; idempotent resume; missing-worktree fallback;
  additive `attempts` migration on an older plane.
- Adapter tests: the pause flag blocks `spawn`/`ready`/`record`/`verify`;
  `session_shutdown` writes the descriptor; `session_start` injects the resume
  prompt only on reason `resume`; `/suspend` clears the flag on resume; a stale
  flag is ignored; registry list, rename, prune, and delete.
- `tests/test_pi_package.py`: the new actions appear in `surface.ACTIONS` and in
  `SLICEME_ACTIONS`.
- End-to-end: suspend after wave 0 and resume to completion; `kill -9` mid-wave
  and resume; concurrent writer check for the descriptor and the optional global
  index; assert no integrated node re-runs and no committed candidate is lost.

## 10. Open questions

- **Naming.** Prefer `/suspend` plus `/campaigns`; do not shadow pi's `/resume`.
- **Park versus exit.** Park and notify by default; auto-exit is opt-in.
  `ctx.shutdown()` is available in all contexts, including command handlers
  (`ExtensionCommandContext` extends `ExtensionContext`), so auto-exit is
  straightforward; the default stays park-and-notify.
- **Registry authority.** The resume descriptor is authoritative for the pi
  binding. A global cache is
  optional and should be per-campaign fragments, not one shared JSON document.
- **Paused worktree retention.** Disk cost versus restart cost; add a
  time-boxed prune and surface it in `gc`.
- **Session-to-campaign cardinality.** One campaign per (plane, target branch);
  a pi session may outlive a campaign, and a campaign may span sessions after a
  `/fork`.
- **`PI_AGENT_DIR` versus `PI_CODING_AGENT_DIR`.** `common.ts::agentDir()` uses
  the former; pi documents the latter. Align one way before adding a global
  index that depends on it.
