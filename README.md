# Sliceme

*Slice a design into parallel coding agents.*

Sliceme turns a design document into a DAG of work. It runs the
non-conflicting nodes in parallel. It checks each wave against a content
fingerprint. It delivers the result as a pull request against the default
branch (`main`). Sliceme decides ownership at plan time, so parallel agents
never author the same files.

- **Plan-time ownership** — nodes own directories; overlapping subtrees go into
  different waves.
- **Readiness is the spawn gate** — a node starts once every dependency reaches
  `done`; the wave stays a display hint.
- **One campaign worktree** — every wave commits onto the same git worktree
  branch. Sliceme never recreates or rebases the branch between waves, and
  files persist.
- **One combined-tree check** — one synchronous runner checks the recorded wave
  tree and caches the verdict. A resumed node reads the cache.
- **One human gate** — the user confirms the campaign one time. Sliceme then
  pushes the campaign branch and opens one pull request against the default
  branch (`main`). Sliceme never pushes the default branch.
- **Deterministic evidence** — the engine writes the campaign commits, checks,
  diffs, and worker logs to one evidence document. The document is the pull
  request body. There is no narrative agent.
- **One workflow resource** — the `sliceme.campaign` pi-subagents resource owns
  the campaign loop. The extension registers the loop, the plan, and delivery.

## Install

```bash
pi install ./                    # or: pi install git:github.com/ming6ao/sliceme
pi
```

`/sliceme [DESIGN.md]` (default `DESIGN.md`) activates the `sliceme` tool for
the session and starts a campaign. The tool invokes the bundled engine, so there
is no `pip install` and no `sliceme` on `PATH`.

## Use

In pi, run `/sliceme DESIGN.md`. The coordinator starts the campaign resource
with `subagent({ workflow: "sliceme.campaign", async: true })`. The resource
drives the engine verbs:

```text
sliceme start DESIGN.md           derive the campaign branch; planner -> dag.json + waves
sliceme wave --open               fetch main; create the worktree from origin/main
sliceme ready                     current-wave nodes whose dependencies are done
sliceme status                    waves, nodes, and paused
sliceme wave --record --current   commit the finished nodes as per-node commits
sliceme check --current           run the combined-tree checks for the wave
sliceme evidence                  write the deterministic evidence document
sliceme review --decision approve record the one campaign approval
sliceme deliver                   push the campaign branch and open a pull request against main
```

## Docs

- [Guide](./docs/guide.md) — model, ownership, orchestration, agent roles.
- [Architecture](./docs/architecture.md) — layered parts, campaign lifecycle, and state (with diagrams).
- [Reference](./docs/reference.md) — actions, modules, state, verification.
- [Workflow](./docs/workflow.md) — the campaign loop, the resource, and the worker contract.
- [Database](./docs/database.md) — the local plane's SQLite schema and lifecycle.
- [Observability](./docs/observability.md) — status projections and check counts.
- [Sessions](./docs/sessions.md) — suspend and resume sessions and campaign progress.
- [Local review](./docs/review.md) — the campaign approval gate and the report.
- [Fake plane](./docs/fake-plane.md) — build a fake plane for engine inspection.
- [Publishing](./docs/publishing.md) — packaging and release.

## Develop

The engine is dependency-free Python 3.11+; the pi adapter is TypeScript.

```bash
npm test
```

When you add an engine action, update `sliceme/surface.py` (the source of
truth); the CLI and the pi tool follow.
