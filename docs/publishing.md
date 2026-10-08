# Publishing and releasing

Sliceme ships as **one pi package** (npm) and, secondarily, as a Python wheel.
The npm tarball is the real distribution unit. It bundles the TypeScript pi
extension, the `/sliceme` command, the GPU host runner, and the entire
dependency-free Python engine. So `pi install npm:sliceme` needs no `pip install`
and no `sliceme` on `PATH`.

## What ships where

| Artifact | Command | Contains |
|---|---|---|
| npm pi package (primary) | `npm pack` / `npm publish` | `integrations/pi/*` (the tool, the resource, and the agents), `docs/`, `tools/gpu.sh`, `bin/sliceme`, `sliceme/*.py`, `README.md`, `LICENSE` |
| Python wheel (secondary) | `python3 -m build` | only the `sliceme` Python package (engine library); **no** `bin/`, `tools/`, or pi extension |

The `files` array in `package.json` is an allow-list with negations; keep the
`!sliceme/__pycache__` / `!sliceme/**/*.pyc` entries so bytecode is not packed.

The Python wheel is engine-only by design (there is no `[project.scripts]`). A
pip-installed engine resolves its GPU runner from the package tree. A wheel-only
deployment must therefore provide `gpu.command` in a sandbox manifest, or put a
`sliceme-gpu` runner on `PATH`.

## Versioning

Keep the version in lockstep across:

- `package.json` → `version`
- `pyproject.toml` → `[project].version`
- `sliceme/__init__.py` → `__version__`

`npm version patch|minor|major` updates only `package.json` (and creates a git
tag), so update the other two in the same commit.

## Publish to npm (the pi package)

```bash
npm login                         # once, on the publishing machine
# bump package.json, pyproject.toml, sliceme/__init__.py together
npm test                          # full suite; also runs via prepublishOnly
npm pack --dry-run                # inspect the exact tarball contents
npm publish                       # (use --dry-run first if unsure)
```

`prepublishOnly` runs `npm test`, so a red suite blocks the publish. The
`pi-package` keyword makes the package eligible for the pi package gallery.
Verify the packed contents before publishing:

```bash
npm pack --dry-run | sed -n '/Tarball Contents/,/Tarball Details/p'
```

## Publish to PyPI (the engine library)

```bash
python3 -m pip install --upgrade build twine
python3 -m build                  # writes dist/*.whl and dist/*.tar.gz
python3 -m twine check dist/*
python3 -m twine upload dist/*    # or: twine upload --repository testpypi dist/*
```

## Use it

Install the package and start a coordinator session:

```bash
pi install npm:sliceme            # registry
pi install git:github.com/ming6ao/sliceme   # a pinned git ref
pi install ./                     # a local checkout
pi                                # launch pi
```

Then, inside the session:

```text
/sliceme DESIGN.md                # activate the tool and start a campaign
sliceme start DESIGN.md           # choose the target branch; planner -> dag.json
sliceme plan --design DESIGN.md   # the campaign split and the next entry
subagent(workflow: "sliceme.campaign", async: true)   # the campaign loop
sliceme review --decision approve # record the campaign approval
sliceme deliver                   # push the campaign branch and open the pull request
sliceme review --report --narrative "..."  # the deterministic report plus your summary
```

Local development without installing:

```bash
pi -e ./                          # load the package for one invocation
SLICEME_BIN=/path/to/bin/sliceme pi
```

## Release checklist

1. `npm test` passes.
2. `package.json`, `pyproject.toml`, and `sliceme/__init__.py` versions match.
3. `npm pack --dry-run` shows no `__pycache__`/`.pyc` and includes `bin/`,
   `tools/`, `sliceme/`, `docs/`, and `integrations/pi/`.
4. `npm publish` (and, if shipping the library, `python3 -m build && twine upload`).
5. Tag the release and push; point the README/`pi install` examples at it.
