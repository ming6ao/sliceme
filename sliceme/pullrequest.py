"""Open a delivery pull request with the ``gh`` command line program.

Sliceme never merges a campaign locally.  Delivery pushes the campaign worktree
branch and opens one pull request against the target feature branch.  The
``gh`` program is the only forge client.  The engine shells out to it, so the
Python package keeps no network dependency of its own.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .util import SlicemeError

__all__ = ["available", "create", "find"]


def available() -> bool:
    """Whether the ``gh`` program is on ``PATH``."""
    return shutil.which("gh") is not None


def _run(root: Path, args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    if not available():
        raise SlicemeError(
            "the `gh` program is not installed; install GitHub CLI to deliver "
            "with a pull request"
        )
    proc = subprocess.run(
        ["gh", *args], cwd=str(root), capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
        raise SlicemeError(f"gh {' '.join(args)} failed: {detail}")
    return proc


def _parse(proc: subprocess.CompletedProcess[str]) -> dict[str, Any] | None:
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("url"):
        return None
    return {"url": str(data["url"]), "number": data.get("number")}


def find(root: Path, head: str) -> dict[str, Any] | None:
    """The pull request for *head*, or ``None`` when there is none."""
    proc = _run(
        root,
        ["pr", "view", head, "--json", "url,number"],
        check=False,
    )
    if proc.returncode != 0:
        return None
    return _parse(proc)


def create(
    root: Path, *, head: str, base: str, title: str, body: str
) -> dict[str, Any]:
    """Create one pull request and return its URL and number."""
    proc = _run(
        root,
        [
            "pr",
            "create",
            "--head",
            head,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        ],
        check=True,
    )
    url = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""
    found = find(root, head)
    if found is not None:
        return found
    if not url:
        raise SlicemeError("gh pr create reported no pull request URL")
    return {"url": url, "number": None}
