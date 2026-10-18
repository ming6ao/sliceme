# Sliceme

*Slice a design into parallel coding agents.*

Sliceme turns a design document into a DAG of work. It runs the
non-conflicting nodes in parallel. It checks each phase against a content
fingerprint. It delivers the result as a pull request against the default
branch (`main`). Sliceme decides ownership at plan time, so parallel agents
never author the same files.

- **Plan-time ownership** — every node owns one directory, and every directory
  has one node. Overlapping subtrees go into different phases.
- **One worker per directory** — the planner splits a multi-directory change
  into one node per directory, and merges two visits of one directory. One
  subagent runs one directory slice.
- **The phase is the scheduling gate** — the engine packs nodes into phases (no
  directory-subtree overlap, capped by `concurrency`) and spawns one phase at a
  time; a recorded node satisfies its dependents' `depends_on`.
- **One campaign worktree** — every phase commits onto the same git worktree
  branch. Sliceme never recreates or rebases the branch between phases, and
  files persist.
- **One combined-tree check** — one synchronous runner checks the recorded phase
  tree and caches the verdict. A resumed node reads the cache.
- **One human gate** — the user confirms the campaign one time. Sliceme then
  delivers in one of two modes. The `pull_request` mode pushes the work branch
  and opens one pull request against the base branch (`main`). The `local`
  mode merges the work branch into the local base branch, and the user pushes
  it later. Sliceme never pushes the default branch.
- **Deterministic evidence** — the engine writes the campaign commits, checks,
  diffs, and worker logs to one evidence document. The document is the pull
  request body. There is no narrative agent.
- **Two workflow resources** — the `sliceme.plan` resource owns the bounded
  planner re-pass loop, and the `sliceme.campaign` resource owns the campaign
  loop. The extension registers both, plus the plan and delivery steps.
- **Optional budget stop** — `start --budget-seconds N --budget-usd USD` stops
  the campaign at a phase boundary when the wall clock or the cost cap is
  reached. The campaign then suspends and resumes through the session path.

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
sliceme start DESIGN.md           derive the work branch; planner -> dag.json + phases
sliceme open                      fetch main; create the worktree from origin/main
sliceme ready                     the current phase's ready node ids
sliceme status                    phases, nodes, and paused
sliceme budget --seconds N --usd USD   set, raise, or clear the campaign caps
sliceme plan DESIGN.md            show the campaign split declared in the design
sliceme record                    commit the finished nodes as per-node commits
sliceme check                     run the combined-tree checks for the phase
sliceme report                    write the deterministic campaign report
sliceme evidence                  write the deterministic evidence document
sliceme deliver --confirm --mode MODE   deliver: pull_request (push + PR) or local (merge the base branch)
```

## Docs

- [Guide](./docs/guide.md) — model, ownership, orchestration, agent roles.
- [Directory slices plan](./docs/directory-slices-plan.md) — the one-node-per-directory target design.
- [Architecture](./docs/architecture.md) — layered parts, campaign lifecycle, and state (with diagrams).
- [Reference](./docs/reference.md) — actions, modules, state, verification.
- [Workflow](./docs/workflow.md) — the campaign loop, the resource, and the worker contract.
- [Database](./docs/database.md) — the local plane's SQLite schema and lifecycle.
- [Observability](./docs/observability.md) — status projections and check counts.
- [Sessions](./docs/sessions.md) — suspend and resume sessions and campaign progress.
- [Local review](./docs/review.md) — the campaign confirmation and delivery, and the report.
- [Fake plane](./docs/fake-plane.md) — build a fake plane for engine inspection.
- [Publishing](./docs/publishing.md) — packaging and release.

## Develop

The engine is dependency-free Python 3.11+; the pi adapter is TypeScript.

```bash
npm test
```

When you add an engine action, update `sliceme/surface.py` (the source of
truth); the CLI and the pi tool follow.
