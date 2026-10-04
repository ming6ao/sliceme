# Fake plane for local review tests

Status: development tool. This tool is not part of a campaign.

The script `tools/fake_plane.py` builds a fake Sliceme plane. Use the fake
plane to test the local review client. The client then has commits, evidence,
comments, and a report to show.

## What the script builds

The script creates a small git repository. It also creates a campaign worktree
and three waves. Each wave records commits on the campaign branch.

The plane contains:

- five commits across three waves;
- nine changed files;
- verification evidence for each commit;
- three review comments;
- one approved commit and one rejected commit;
- a campaign report;
- five subagent run records;
- two session descriptors.

The evidence includes one passed run, one failed run, and one error. The script
also adds an older failed job. The newest job wins in the evidence panel.

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

The script prints a summary. The summary shows the commit count, the file
count, the comment count, and the approval state.

## Serve the review client

Start the server and point it at the fake plane.

```bash
python3 -m sliceme review --serve --plane /tmp/sliceme-fake-plane
```

You can also use the bundled shim.

```bash
./bin/sliceme review --serve --plane /tmp/sliceme-fake-plane
```

The server prints a URL. The URL holds a write token. Open the URL in a browser.
The server binds to the loopback address only.

To select a port, use `--port`.

```bash
python3 -m sliceme review --serve --plane /tmp/sliceme-fake-plane --port 8777
```

## Test the client

Use the fake plane to test these functions:

- the commit list and the approval toggle;
- the **Approve all** button;
- the **Deliver** button;
- the file tree and the file diff;
- the report view;
- the comment form and the comment list;
- the evidence panel.

The **Deliver** button stays hidden until every commit has an approval. Click
**Approve all** to approve the other four commits. Then click **Deliver**.

## Remove the plane

The plane lives in one directory. Remove the directory when you finish.

```bash
rm -rf /tmp/sliceme-fake-plane
```

## Related documents

- [Local review](./review.md) — the review client, the server, and the
  approval gate.
- [Database](./database.md) — the SQLite schema and the data lifecycle.
