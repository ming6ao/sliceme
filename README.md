# Sliceme

*Slice a design into parallel coding agents.*

Sliceme turns a design document into a DAG of work. It runs the non-conflicting
nodes in parallel and verifies each node against a content fingerprint. It
delivers the results as a pull request against a target feature branch. Sliceme
decides ownership at plan time, so parallel agents never author the same files.

- **Plan-time ownership** — nodes own directories; overlapping subtrees are
  serialized into waves.
- **Readiness is the spawn gate** — a node starts once every dependency is
  done; the wave stays a display hint.
- **One campaign worktree** — every wave commits onto the same git worktree
  branch. Sliceme never recreates or rebases the branch between waves, and files
  persist.
- **One executor** — a single serialized, sandboxed runner drains a check queue,
  so verifiers judge recorded evidence instead of each running the suite.
- **Approved delivery** — Sliceme merges nothing. Commits accumulate, and a
  human approves each commit in the local review client. When every wave
  finishes and a human approves every commit, Sliceme pushes the campaign branch
  and opens one pull request against the target feature branch. Sliceme refuses
  the default branch (`main`, `master`, or the repository default) with no
  override.

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
sliceme start <DESIGN.md>   choose the target branch; planner -> dag.json + waves
sliceme status              waves, nodes, and live child state
sliceme ready               current-wave nodes whose dependencies are done
sliceme spawn --nodes <ids> start a wave: one-shot pure editors, one worktree
sliceme record              commit the current wave onto the campaign worktree
sliceme verify --nodes <ids> one turn: the executor runs the wave; one verifier judges
sliceme review --serve      local review client (per-commit approval)
sliceme deliver             push the campaign branch and open a pull request
sliceme report              deterministic report plus the narrative
```

## Docs

- [Guide](./docs/guide.md) — model, ownership, orchestration, agent roles.
- [Architecture](./docs/architecture.md) — layered parts, campaign lifecycle, and state (with diagrams).
- [Reference](./docs/reference.md) — actions, modules, state, verification.
- [Workflow](./docs/workflow.md) — the campaign loop and worker contract.
- [Database](./docs/database.md) — the local plane's SQLite schema and lifecycle.
- [Observability](./docs/observability.md) — run visibility and timing/agent metrics (partly implemented).
- [Sessions](./docs/sessions.md) — suspend and resume sessions and campaign progress.
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
