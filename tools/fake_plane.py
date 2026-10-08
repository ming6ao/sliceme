#!/usr/bin/env python3
"""Build a fake Sliceme plane for engine inspection.

The script creates a real git repository with a small project, a campaign
worktree, three recorded waves, check evidence, review decisions, a report,
and suspend/resume descriptors.  The result is a complete plane the engine can
inspect with no campaign run.

Example:

    python3 tools/fake_plane.py --dir /tmp/sliceme-fake
    python3 -m sliceme --root /tmp/sliceme-fake status

The script is destructive: it removes the target directory before it builds
the plane.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sliceme import campaign  # noqa: E402
from sliceme.service import Service  # noqa: E402
from sliceme.util import now, sha256_text, state_dir, write_json  # noqa: E402

FEATURE_BRANCH = "feat/checkout"
DECOY_BRANCH = "feat/notifications"
CAMPAIGN = "checkout-service"

DAG = {
    "campaign": CAMPAIGN,
    "feature_branch": FEATURE_BRANCH,
    "base": "main",
    "concurrency": 2,
    "nodes": [
        {
            "id": "w1",
            "label": "API auth",
            "owns": ["dir:src/api"],
            "depends_on": [],
            "acceptance": ["true"],
        },
        {
            "id": "w2",
            "label": "Store migrations",
            "owns": ["dir:src/store"],
            "depends_on": [],
            "acceptance": ["true"],
        },
        {
            "id": "w3",
            "label": "Web session flow",
            "owns": ["dir:src/web"],
            "depends_on": ["w1"],
            "acceptance": ["true"],
        },
        {
            "id": "w4",
            "label": "CLI flags",
            "owns": ["dir:src/cli"],
            "depends_on": ["w2"],
            "acceptance": ["true"],
        },
        {
            "id": "w5",
            "label": "Usage docs",
            "owns": ["dir:docs"],
            "depends_on": ["w1", "w2"],
            "acceptance": ["true"],
        },
    ],
}

PROJECT = {
    "pyproject.toml": (
        "[project]\n"
        'name = "checkout-service"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        "dependencies = []\n"
    ),
    "README.md": (
        "# Checkout service\n\n"
        "A small example service used to exercise the Sliceme review client.\n"
    ),
    "src/api/__init__.py": "",
    "src/api/handlers.py": (
        '"""Request handlers for the checkout service."""\n\n\n'
        "def handle_checkout(request):\n"
        "    token = request.get('token')\n"
        "    return {'ok': True, 'token': token}\n"
    ),
    "src/api/routes.py": (
        '"""Route table."""\n\n'
        "ROUTES = {\n"
        "    '/checkout': 'handle_checkout',\n"
        "}\n"
    ),
    "src/store/__init__.py": "",
    "src/store/db.py": (
        '"""Tiny in-memory store."""\n\n'
        "ROWS = {}\n\n\n"
        "def put(key, value):\n"
        "    ROWS[key] = value\n"
    ),
    "src/store/models.py": (
        '"""Domain models."""\n\n'
        "from dataclasses import dataclass\n\n\n"
        "@dataclass\n"
        "class Order:\n"
        "    id: str\n"
        "    total: float\n"
    ),
    "src/web/__init__.py": "",
    "src/web/app.py": (
        '"""Minimal web entry point."""\n\n\n'
        "def render(order):\n"
        "    return f'<p>{order.id}</p>'\n"
    ),
    "src/cli/__init__.py": "",
    "src/cli/main.py": (
        '"""Command line entry point."""\n\n'
        "import argparse\n\n\n"
        "def main(argv=None):\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.parse_args(argv)\n"
        "    return 0\n"
    ),
    "docs/usage.md": "# Usage\n\nRun the service locally.\n",
    "tests/test_smoke.py": (
        "def test_import():\n"
        "    import src.api.handlers  # noqa: F401\n"
    ),
}

# One edit set per node.  Every path stays inside the node's owned directory.
NODE_EDITS = {
    "w1": {
        "src/api/handlers.py": (
            '"""Request handlers for the checkout service."""\n\n'
            "import time\n\n\n"
            "def handle_checkout(request):\n"
            "    token = request.get('token')\n"
            "    if not token:\n"
            "        return {'ok': False, 'error': 'missing token'}\n"
            "    return {'ok': True, 'token': token, 'at': time.time()}\n"
        ),
        "src/api/auth.py": (
            '"""Token checks."""\n\n\n'
            "def verify(token):\n"
            "    return bool(token) and len(token) > 8\n"
        ),
    },
    "w2": {
        "src/store/db.py": (
            '"""Tiny in-memory store with a schema version."""\n\n'
            "SCHEMA = 2\n"
            "ROWS = {}\n\n\n"
            "def put(key, value):\n"
            "    ROWS[key] = value\n\n\n"
            "def get(key):\n"
            "    return ROWS.get(key)\n"
        ),
        "src/store/migrations.py": (
            '"""Schema migrations."""\n\n\n'
            "def migrate(store, version):\n"
            "    if version < 2:\n"
            "        store['schema'] = 2\n"
            "    return store\n"
        ),
    },
    "w3": {
        "src/web/app.py": (
            '"""Minimal web entry point with a session cookie."""\n\n\n'
            "def render(order, session=None):\n"
            "    name = (session or {}).get('user', 'guest')\n"
            "    return f'<p>{order.id} for {name}</p>'\n"
        ),
        "src/web/session.py": (
            '"""Session helpers."""\n\n\n'
            "def read_session(request):\n"
            "    return request.get('session') or {}\n"
        ),
    },
    "w4": {
        "src/cli/main.py": (
            '"""Command line entry point with flags."""\n\n'
            "import argparse\n\n\n"
            "def main(argv=None):\n"
            "    parser = argparse.ArgumentParser()\n"
            "    parser.add_argument('--store', default='memory')\n"
            "    parser.add_argument('--verbose', action='store_true')\n"
            "    parser.parse_args(argv)\n"
            "    return 0\n"
        ),
    },
    "w5": {
        "docs/usage.md": (
            "# Usage\n\n"
            "Run the service locally. See the `README.md` file for an overview.\n\n"
            "> The service reads no configuration files yet.\n\n"
            "## Flags\n\n"
            "| Flag | Default | Purpose |\n"
            "| --- | --- | --- |\n"
            "| `--store` | `memory` | Select the store backend. |\n"
            "| `--verbose` | off | Enable debug output. |\n\n"
            "## Example\n\n"
            "```bash\n"
            "python -m src.cli.main --store memory --verbose\n"
            "```\n\n"
            "The **default** store keeps data in memory. A later release adds a\n"
            "*file* backend. Use the [project issues](https://example.com/issues)\n"
            "for a request.\n"
        ),
        "docs/configuration.md": (
            "# Configuration\n\n"
            "The service reads no configuration files yet.\n"
        ),
    },
}

# Evidence per node: status, duration, output, optional error, and commands.
EVIDENCE = {
    "w1": {
        "status": "passed",
        "duration": 12.4,
        "commands": ["pytest tests/test_smoke.py", "ruff check src/api"],
        "output": (
            "pytest tests/test_smoke.py\n"
            "1 passed in 0.42s\n\n"
            "ruff check src/api\n"
            "All checks passed.\n"
        ),
    },
    "w2": {
        "status": "passed",
        "duration": 8.1,
        "commands": ["pytest tests/test_smoke.py"],
        "output": "1 passed in 0.31s\n",
    },
    "w3": {
        "status": "failed",
        "duration": 3.2,
        "commands": ["pytest tests/test_session.py"],
        "output": (
            "pytest tests/test_session.py\n"
            "FAILED tests/test_session.py::test_guest\n"
            "1 failed in 0.18s\n"
        ),
    },
    "w4": {
        "status": "passed",
        "duration": 5.5,
        "commands": ["pytest tests/test_smoke.py", "python -m src.cli.main --help"],
        "output": "1 passed in 0.27s\nusage: main [-h] [--store STORE] [--verbose]\n",
    },
    "w5": {
        "status": "error",
        "duration": 0.4,
        "commands": ["markdownlint docs"],
        "output": "markdownlint docs\nerror: markdownlint is not installed\n",
    },
}


def run(*args: str, cwd: Path) -> None:
    result = subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise SystemExit(
            f"command failed: {' '.join(args)}\n{result.stdout}{result.stderr}"
        )


def write_text(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_git_project(root: Path) -> None:
    run("git", "init", "-q", "-b", "main", cwd=root)
    run("git", "config", "user.email", "demo@sliceme.local", cwd=root)
    run("git", "config", "user.name", "Sliceme Demo", cwd=root)
    for rel, text in PROJECT.items():
        write_text(root, rel, text)
    run("git", "add", "-A", cwd=root)
    run("git", "commit", "-qm", "initial checkout service", cwd=root)


def build_campaign(svc: Service) -> dict[str, str]:
    """Record every wave and return a node -> commit map."""
    commits: dict[str, str] = {}
    messages = {
        "w1": "add token verification and migrations",
        "w2": "add the migration runner",
        "w3": "add the session flow and CLI flags",
        "w4": "add the session resume flags",
        "w5": "document usage and configuration",
    }
    for wave in range(3):
        svc.create_campaign_workspace(base="main")
        members = (
            [("w1", "w2"), ("w3", "w4"), ("w5",)][wave]
        )
        for node in members:
            for rel, text in NODE_EDITS[node].items():
                write_text(Path(svc.store.get_unit("campaign")["worktree"]), rel, text)
        result = svc.record_wave(wave, messages={node: messages[node] for node in members})
        for candidate in result["candidates"]:
            commits[str(candidate["node"])] = str(candidate["head_commit"])
    return commits


def add_evidence(svc: Service, commits: dict[str, str], node_wave: dict[str, int]) -> None:
    """Insert one terminal check per commit, plus one superseded failure."""
    store = svc.store
    # A superseded failure for w2 first, so the later passing check is the newest
    # terminal row and wins in the evidence panel.
    store.create_check(
        fingerprint=sha256_text(f"stale:{commits['w2']}"),
        source="node:w2",
        commit_ref=commits["w2"],
        status="failed",
        commands=["pytest tests/test_smoke.py"],
        wave=node_wave["w2"],
        duration=1.0,
        exit_code=1,
        output="pytest tests/test_smoke.py\n1 failed in 0.11s\n",
    )
    for node, spec in EVIDENCE.items():
        commit = commits[node]
        store.create_check(
            fingerprint=sha256_text(f"{node}:{commit}:{spec['status']}"),
            source=f"node:{node}",
            commit_ref=commit,
            status=spec["status"],
            commands=list(spec["commands"]),
            wave=node_wave[node],
            duration=spec["duration"],
            exit_code=0 if spec["status"] == "passed" else 1,
            output=spec["output"],
        )
    store.conn.commit()


def add_review_data(svc: Service, commits: dict[str, str]) -> None:
    svc.review_decision(
        action="approve", commit=commits["w1"], actor="demo-reviewer"
    )
    svc.review_decision(
        action="request_changes",
        commit=commits["w3"],
        actor="demo-reviewer",
        note="Fix the failing session test before approval.",
    )


def write_state(root: Path, config: dict, commits: dict[str, str]) -> None:
    state = {
        "campaign": CAMPAIGN,
        "feature_branch": FEATURE_BRANCH,
        "target_branch": FEATURE_BRANCH,
        "worktree_branch": config.get("worktree_branch"),
        "base": "main",
        "delivery_base": "main",
        "delivered": False,
        "wave_size": 2,
        "current_wave": 2,
        "waves": [
            {
                "index": 0,
                "members": ["w1", "w2"],
                "status": "done",
                "integrated": ["w1", "w2"],
                "cleanup_done": True,
            },
            {
                "index": 1,
                "members": ["w3", "w4"],
                "status": "done",
                "integrated": ["w3", "w4"],
                "cleanup_done": True,
            },
            {
                "index": 2,
                "members": ["w5"],
                "status": "done",
                "integrated": ["w5"],
                "cleanup_done": True,
            },
        ],
        "nodes": {
            node: {
                "status": "done",
                "attempts": 1,
                "commit": commit,
                "wave": wave,
            }
            for wave, (node, commit) in enumerate(
                [
                    ("w1", commits["w1"]),
                    ("w2", commits["w2"]),
                    ("w3", commits["w3"]),
                    ("w4", commits["w4"]),
                    ("w5", commits["w5"]),
                ]
            )
        },
    }
    write_json(campaign.state_path(root, FEATURE_BRANCH), state)


def write_session_descriptors(root: Path, config: dict, commits: dict[str, str]) -> None:
    """Write a ready descriptor for the real campaign and a suspended decoy."""
    state_dir(root).mkdir(parents=True, exist_ok=True)
    ready = {
        "campaign": CAMPAIGN,
        "feature_branch": FEATURE_BRANCH,
        "worktree_branch": config.get("worktree_branch"),
        "design": "DESIGN.md",
        "pi": {
            "session_id": "demo-session-0001",
            "session_file": "/home/demo/.pi/agent/sessions/demo/0001.jsonl",
            "cwd": str(root),
        },
        "label": CAMPAIGN,
        "status": "ready",
        "reason": "user",
        "suspended_at": now() - 3600.0,
        "current_wave": 2,
        "waves": [
            {"index": 0, "members": ["w1", "w2"], "status": "done"},
            {"index": 1, "members": ["w3", "w4"], "status": "done"},
            {"index": 2, "members": ["w5"], "status": "done"},
        ],
        "nodes": {
            node: {
                "status": "done",
                "attempt": 1,
                "candidate": None,
                "commit": commits[node],
            }
            for node in commits
        },
        "resume_plan": {
            "record_wave": None,
            "resume": [],
            "respawn": [],
            "verify": [],
            "blocked": [],
        },
    }
    write_json(campaign.session_path(root, FEATURE_BRANCH), ready)

    decoy = {
        "campaign": "notifications-service",
        "feature_branch": DECOY_BRANCH,
        "worktree_branch": DECOY_BRANCH,
        "design": "DESIGN.md",
        "pi": {
            "session_id": "demo-session-0002",
            "session_file": "/home/demo/.pi/agent/sessions/demo/0002.jsonl",
            "cwd": str(root),
        },
        "label": "notifications",
        "status": "suspended",
        "reason": "user",
        "suspended_at": now() - 600.0,
        "current_wave": 1,
        "waves": [
            {"index": 0, "members": ["n1", "n2"], "status": "done"},
            {"index": 1, "members": ["n3"], "status": "running"},
        ],
        "nodes": {
            "n1": {"status": "done", "attempt": 1, "candidate": 9001, "commit": "0" * 40},
            "n2": {"status": "done", "attempt": 1, "candidate": 9002, "commit": "1" * 40},
            "n3": {"status": "paused", "attempt": 2, "candidate": None, "commit": None},
        },
        "resume_plan": {
            "record_wave": 1,
            "resume": ["n3"],
            "respawn": [],
            "verify": ["n3"],
            "blocked": [],
        },
    }
    write_json(campaign.session_path(root, DECOY_BRANCH), decoy)


def write_worker_logs(root: Path, commits: dict[str, str]) -> None:
    for node in commits:
        log = campaign.worker_log_path(root, FEATURE_BRANCH, node)
        log.write_text(
            f"[demo] worker {node} finished\n", encoding="utf-8"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        default="/tmp/sliceme-fake-plane",
        help="target directory (removed first; default: /tmp/sliceme-fake-plane)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="do not remove an existing target directory",
    )
    args = parser.parse_args()

    root = Path(args.dir).expanduser().resolve()
    if root.exists():
        if not args.keep:
            shutil.rmtree(root)
        elif (root / ".sliceme" / "config.json").is_file():
            raise SystemExit(
                f"{root} already holds a plane; remove it or pick another --dir"
            )
    root.mkdir(parents=True, exist_ok=True)

    build_git_project(root)
    config = Service.init_plane(
        root,
        feature_branch=FEATURE_BRANCH,
        base="main",
        checks=[{"name": "smoke", "command": "true", "required": True}],
    )
    write_json(campaign.dag_path(root, FEATURE_BRANCH), DAG)

    svc = Service(root)
    try:
        commits = build_campaign(svc)
        node_wave = {"w1": 0, "w2": 0, "w3": 1, "w4": 1, "w5": 2}
        add_evidence(svc, commits, node_wave)
        add_review_data(svc, commits)
        svc.report(
            narrative=(
                "The campaign adds token verification, store migrations, a "
                "session-aware web render, and CLI flags."
            ),
            design="DESIGN.md",
        )
        write_state(root, config, commits)
        write_session_descriptors(root, config, commits)
        write_worker_logs(root, commits)
        snapshot = svc.review_snapshot()
    finally:
        svc.close()

    print(f"fake plane: {root}")
    print(f"  feature branch: {FEATURE_BRANCH}")
    print(f"  commits: {len(snapshot['commits'])}")
    print(f"  files: {len(snapshot['files'])}")
    print(f"  all approved: {snapshot['all_approved']}")
    print()
    print("inspect the plane with:")
    print(f"  python3 -m sliceme --root {root} status")
    print(
        f"  python3 -m sliceme --root {root} review --report --campaign {FEATURE_BRANCH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
