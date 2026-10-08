# Fake plane for engine tests

Status: development tool. This tool is not part of a campaign.

The script `tools/fake_plane.py` builds a fake Sliceme plane. Use the fake plane
to inspect the engine and the report without a campaign run. Sliceme removed the
browser review client, so the fixture targets the reduced review surface.

## What the script builds

The script creates a small git repository. It also creates a campaign worktree
and three waves. Each wave records commits on the campaign branch.

The plane contains:

- five commits across three waves;
- nine changed files;
- check evidence for each commit;
- campaign review decisions;
- a campaign report;
- worker logs;
- two suspend/resume descriptors.

## Requirements

- Python 3.11 or later.
- `git` on the `PATH`.
- A checkout of this repository.

## Build the plane

Run the script from the repository root.

```bash
python3 tools/fake_plane.py
```

The default directory is `/tmp/sliceme-fake-plane`. The script removes this
directory first.

To select another directory, use `--dir`.

```bash
python3 tools/fake_plane.py --dir /tmp/my-plane
```

To keep an existing directory, use `--keep`. The script stops when the
directory already holds a plane.

```bash
python3 tools/fake_plane.py --dir /tmp/my-plane --keep
```

The script prints a summary: the feature branch, the commit count, the changed
file count, and the campaign approval state. It also prints the engine commands
that inspect the plane.

## Inspect the plane

Use the engine verbs against the fake plane root.

```bash
python3 -m sliceme --root /tmp/sliceme-fake-plane status
python3 -m sliceme --root /tmp/sliceme-fake-plane status --verbose
python3 -m sliceme --root /tmp/sliceme-fake-plane review --report --campaign feat/checkout
```

The report path from the summary holds the deterministic campaign report.

## Remove the plane

The plane lives in one directory. Remove the directory when you finish.

```bash
rm -rf /tmp/sliceme-fake-plane
```

## Related documents

- [Campaign review](./review.md) — the approval gate and the report.
- [Database](./database.md) — the SQLite schema and the data lifecycle.
