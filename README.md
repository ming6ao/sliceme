# Sliceme

*Slice a design into parallel coding agents.*

Sliceme turns a design document into a DAG of work. It runs the non-conflicting
nodes in parallel and verifies each node against a content fingerprint. It
delivers the results onto a target feature branch. Sliceme decides ownership at
plan time, so parallel agents never author the same files.

- **Plan-time ownership** — nodes own directories; overlapping subtrees are
  serialized into waves.
- **One campaign worktree** — every wave commits onto the same git worktree
  branch. Sliceme never recreates or rebases the branch between waves, and files
  persist.
- **One executor** — a single serialized, sandboxed runner drains a check queue,
  so verifiers judge recorded evidence instead of each running the suite.
- **Approved delivery** — Sliceme merges nothing per wave. Commits accumulate,
  and a human approves each commit in the local review client. When every wave
  finishes and a human approves every commit, the coordinator runs one `--no-ff`
  merge into the target feature branch. Sliceme refuses the default branch
  (`main`, `master`, or the repository default) with no override.

## Install

```bash
pi install ./                    # or: pi install git:github.com/ming6ao/sliceme
pi
```

`/sliceme [DESIGN.md]` (default `DESIGN.md`) activates the `sliceme` and
`sliceme-unit` tools for the session and starts a campaign. The tools invoke the
bundled engine, so there is no `pip install` and no `sliceme` on `PATH`.

## Use

In pi, run `/sliceme DESIGN.md`. The coordinator then drives the campaign with
the `sliceme` tool:

```text
sliceme start <DESIGN.md>   choose target branch; planner -> dag.json + waves
sliceme ready               current-wave nodes whose dependencies are done
sliceme spawn <node>        one-shot pure editor in the campaign worktree
sliceme record               commit the current wave onto the campaign worktree
sliceme verify <node>       executor runs checks; a read-only verifier judges
sliceme review --serve      local review client (per-commit approval)
sliceme deliver             merge to target once every commit is approved
sliceme review --report     deterministic report
```

## Docs

- [Guide](./docs/guide.md) — model, ownership, orchestration, agent roles.
- [Architecture](./docs/architecture.md) — layered parts, campaign lifecycle, and state (with diagrams).
- [Reference](./docs/reference.md) — actions, modules, state, verification.
- [Workflow](./docs/workflow.md) — the campaign loop and worker contract.
- [Database](./docs/database.md) — the local plane's SQLite schema and lifecycle.
- [Observability](./docs/observability.md) — proposed design for run visibility and timing/agent metrics.
- [Sessions](./docs/sessions.md) — proposed design for suspending and resuming sessions and campaign progress.
- [Local review](./docs/review.md) — the local review client, server, and per-commit approval gate.
- [Fake plane](./docs/fake-plane.md) — build a fake plane for local review-client tests.
- [Publishing](./docs/publishing.md) — packaging and release.

## Develop

The engine is dependency-free Python 3.11+; the pi adapter is TypeScript.

```bash
python3 -m unittest discover -s tests -v
```

To test the review client without a campaign, build a fake plane and serve it.

```bash
python3 tools/fake_plane.py --dir /tmp/sliceme-fake-plane
python3 -m sliceme review --serve --plane /tmp/sliceme-fake-plane
```

See [Fake plane](./docs/fake-plane.md) for the details.

When adding an engine action, update `sliceme/surface.py` (the source of truth);
the CLI and pi tools follow.
