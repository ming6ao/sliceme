# Sliceme session suspend and resume

Status: implemented, with two items open. The engine `status --resume` and
`status --sessions` actions, the `attempt` action, the adapter descriptor and
pause flag, the `/suspend` and `/campaigns` commands, and wave-scoped
continuation all work. Budget auto-suspend and a paused-worktree prune policy
remain future work. The descriptor file is the only registry: there is no
`campaign_sessions` table.

This document describes how a pi session suspends on request and resumes later.
It also describes how a long campaign checkpoints its progress, so a resume
continues instead of restarts.

Pi saves every session as JSONL and reopens it with `pi --continue`,
`pi --resume`, and `/resume`. The transcript needs no new storage.

## 1. Goals

- A user can suspend a pi session at a safe point, without loss of committed or
  uncommitted campaign work.
- A suspended session is discoverable, nameable, and resumable from the same
  checkout, from a second terminal, or after a process crash.
- A long campaign resumes from the wave where it stopped. Sliceme never re-runs
  a done node. Committed candidates re-verify from cache. A node whose worker
  stopped during the suspension continues from the preserved campaign worktree.
- The user can store and manage suspended sessions as campaigns.

### Non-goals

- Replacing the pi session picker. `/resume` stays pi's.
- A daemon. Suspension is a file and SQLite checkpoint plus the pi session file.
- Remote or cross-repository scheduling.

One session owns one campaign. Several campaigns can share a plane at the same
time. The session records its campaign in `.sliceme/active.<pid>.campaign`, and
every campaign-scoped engine call passes `--campaign <branch>`.

## 2. Two layers, one lifecycle

```text
                 /suspend                 pi --continue / /campaigns
   active ───────────────▶ checkpointing ─────────▶ suspended
     ▲   (pause flag +      (adapter checkpoint)        │
     │    abort)                                        │
     └───────────── resume (switchSession + auto-prompt)┘
```

- **Pi session layer.** Pi autosaves the transcript as JSONL. Suspension reaches
  a safe point, parks, and registers. Resume reopens the transcript and injects
  a continuation prompt.
- **Campaign layer.** `dag.json`, `state.json`, `state.db`, and git already
  describe campaign progress. The resume descriptor adds what they cannot:
  which pi session to reopen, why the pause happened, and which wave to record
  on resume.

## 3. Commands

| Command | Behavior |
|---|---|
| `/sliceme [DESIGN.md]` | The start entry point. It activates the tools and starts a campaign. It refuses while the agent is busy (`ctx.isIdle()`). |
| `/suspend [label]` | Parks the current session: writes the pause control flag, aborts the in-flight turn, waits for idle, and writes the resume descriptor. An interrupted node becomes `paused`, so resume continues its edits. |
| `/campaigns` | An interactive list of registered campaigns: label, target branch, wave, done/total, last activity, status. Actions: resume, show, prune, delete. |

Pi owns `/resume`, so the adapter does not register it. The `session_start`
hook with reason `"resume"` provides the campaign hook instead.

`/suspend` runs in an `ExtensionCommandContext`. That type adds `waitForIdle()`,
`switchSession()`, `newSession()`, `fork()`, and `reload()` on top of
`ExtensionContext` (`shutdown()`, `abort()`, `signal`, `isIdle()`, `hasUI`).
`/campaigns` uses `switchSession()` to reopen a saved session.

### How `/suspend` stops work

1. `/suspend` writes `.sliceme/<branch-key>.control.json` with `pause: true` and
   a timestamp.
2. It calls `ctx.abort()` when the agent is not idle. `ctx.abort()` aborts the
   tool signal. That call kills the current worker subagent and any engine
   subprocess, so suspension lands at the next safe point within seconds.
3. It calls `await ctx.waitForIdle()`.
4. It clears the pause flag and writes the descriptor.

The adapter does not use a steering message. A steering message arrives only at
the next turn boundary, after the node has finished.

### Event hooks

The adapter registers two hooks:

- `session_start` (reasons `startup` and `resume`) detects a suspended campaign
  for the current branch. It re-activates the campaign tools. Pi does not
  restore the active tool set from the transcript on resume, so this step is
  necessary. It injects the resume prompt on `resume` and offers it on
  `startup`.
- `session_shutdown` writes the descriptor synchronously and idempotently, with
  no subprocess. The descriptor status becomes `completed` after a delivery. It
  becomes `ready` when every wave finished but delivery did not happen.
  Otherwise it becomes `suspended`.

`pi.appendEntry("sliceme.session", descriptor)` records a compact copy in the
transcript.

Two further hooks are possible but not registered: `session_before_switch` (a
guard before a switch) and `agent_settled` (a turn-boundary checkpoint).

## 4. Storage

| Store | Path | Owner | Role |
|---|---|---|---|
| Resume descriptor | `.sliceme/<branch-key>.session.json` | pi adapter | pi session binding, pause reason, wave, resume plan. |
| Pause flag | `.sliceme/<branch-key>.control.json` | pi adapter | Cooperative pause request. |
| Transcript | pi session JSONL | pi | Conversation history. |

Suspension is a pi-session concern. There is deliberately no engine `suspend`
action, because two rich writers in two languages for one file is a failure
mode. A second terminal inspects campaigns with `sliceme status --sessions` and
resumes by starting pi.

The design uses no global index. Discovery reads the descriptor files.
`agentDir()` in `integrations/pi/common.ts` reads `PI_AGENT_DIR`, but pi
documents `PI_CODING_AGENT_DIR`. Align the two before any global index depends
on them.

## 5. Engine actions

`sliceme/surface.py` owns these actions:

```text
sliceme status --resume  [--plan-only]
sliceme status --sessions
```

- `status --resume` reads `.sliceme/<branch-key>.session.json`, reconciles it
  with git plus `state.db`, and returns the resume plan. `--plan-only` reports
  without side effects.
- `status --sessions` is the engine view of the registered campaigns for a
  second terminal. It reads the descriptor files.

The adapter writes the descriptor. The engine reads it.

## 6. Resume descriptor

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

The descriptor does not duplicate the per-node status of `state.json`. It
records the status at suspend time plus the recorded commit, so resume can
detect drift. There is no per-node worktree or branch: every node in a wave
shares the campaign worktree. Each campaign has its own descriptor file, keyed
by the campaign branch.

A successful `deliver` writes `completed` immediately, so a crash after the
pull request cannot leave a `suspended` descriptor. When every wave finished but
delivery did not happen, `session_shutdown` writes `ready`, because no wave
remains to continue. As a backstop, `session_start` re-derives the plan from the
engine before it offers or injects a resume. If the plane shows no remaining
work, it rewrites the descriptor to `completed` or `ready` and stays silent.

## 7. Node states and reconciliation

The node states are `pending`, `running`, `recorded`, `done`, `failed`, and
`paused`. The code declares the `stopped` state but nothing writes it.

On resume, reconcile from plane evidence, which wins over the descriptor:

| Plane evidence | Resume status |
|---|---|
| latest candidate `landed` | `done`; never re-run |
| node `done` and recorded commit equals the latest candidate `head_commit` | `done`; re-verify from cache only |
| candidate `prepared` with a recorded commit | `recorded`; verify |
| candidate `prepared` but no attributable commit for the node | `pending`; re-spawn |
| node `running` or `paused` and the campaign worktree is present | `paused`; record, then continue |
| node `running` or `paused` but the campaign worktree does not exist | `pending`; fresh spawn |
| executor job `running` past its lease | requeued by `recover_orphan_jobs` |

The commit comparison is essential. A verifier can pass a node that delivery
did not land. That node is `done` in `state.json`, but its candidate is still
`prepared`, because verification is a separate table and never changes the
candidate row. A status-only rule would wrongly re-run that node.

## 8. Wave-scoped continuation

The campaign worktree persists across a suspend. A `paused` node is a node whose
worker the suspension interrupted before `record`. On resume the coordinator:

1. runs `wave --record --wave N` under the executor lock, which commits the
   edits and attributes every changed path to exactly one node by owned
   directories;
2. re-verifies a node when its prepared candidate fingerprint did not change
   (the executor returns `cached`, so no check re-runs);
3. re-spawns a node that produced no attributable changes.

`_record_wave_commits` diffs against `HEAD`, so a later record cannot
re-attribute an earlier wave.

A continuation worker starts in the same campaign worktree. Its prompt holds
the node goal and acceptance, `git status --porcelain`, `git diff --stat`, the
last heartbeat, and the previous verifier evidence.

## 9. Cooperative pause

1. `/suspend` writes the control flag and aborts the in-flight turn.
2. `spawn`, `ready`, `ensureWaves`, `record`, and `verify` check the flag and
   return a `paused` result instead of doing work.
3. The adapter writes the descriptor when the turn settles.
4. Resume clears the pause flag before any other step.
5. A hard abort (Escape, Ctrl+C) still works: `ctx.signal` propagates through
   `runSubagent` to a SIGTERM of the child, then a SIGKILL after five seconds.

## 10. Remaining work

- Budget auto-suspend from a wall-clock or cost cap.
- A time-boxed prune policy for preserved campaign worktrees, surfaced in `gc`.

## 11. Test plan

`tests/test_sessions.py` covers the descriptor round-trip, the resume mapping
(including the `landed` and verified-but-undelivered cases), the
preserved-worktree path, the missing-worktree fallback, idempotent resume, and
the additive `attempts` migration. Adapter tests cover the pause flag, the
hooks, the resume prompt, and the registry.
