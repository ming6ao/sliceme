"""Open a delivery pull request with the ``gh`` command line program.

Sliceme never merges a campaign locally.  Delivery pushes the campaign worktree
branch and opens one pull request against the target feature branch.  The
``gh`` program is the only forge client.  The engine shells out to it, so the
Python package keeps no network dependency of its own.

The review server and the coordinator run as background children.  A process
started from a desktop launcher or a service often has a small ``PATH`` that
omits the user-local and Homebrew directories.  This module resolves ``gh`` in
three steps.  It checks ``SLICEME_GH`` first, then ``PATH``, then the common
install directories.  A missing ``PATH`` entry therefore does not mean that
GitHub CLI is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .util import SlicemeError

__all__ = ["available", "create", "find", "require", "resolve"]

#: Directories where GitHub CLI is commonly installed.  ``PATH`` is still
#: searched first, so these are only a fallback for a small process ``PATH``.
_COMMON_BIN_DIRS = (
    "~/.local/bin",
    "~/.linuxbrew/bin",
    "/home/linuxbrew/.linuxbrew/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    "/snap/bin",
    "/usr/bin",
    "/bin",
)

#: Windows install locations, used when sliceme runs on Windows.  On Windows
#: Subsystem for Linux, ``PATH`` interop finds ``gh.exe`` instead.
_WINDOWS_GH_PATHS = (
    r"%ProgramFiles%\GitHub CLI\gh.exe",
    r"%ProgramFiles(x86)%\GitHub CLI\gh.exe",
    r"%LOCALAPPDATA%\Programs\GitHub CLI\gh.exe",
)


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _which(name: str) -> str | None:
    """Locate *name* on ``PATH``; the indirection keeps tests isolated."""
    return shutil.which(name)


def _candidate_paths() -> list[Path]:
    """Every path that can hold the ``gh`` program, in precedence order."""
    # ``gh.exe`` covers Windows and Windows Subsystem for Linux interop.
    names = ("gh", "gh.exe")
    candidates: list[Path] = []

    override = os.environ.get("SLICEME_GH")
    if override:
        candidates.append(Path(override).expanduser())

    for name in names:
        found = _which(name)
        if found:
            candidates.append(Path(found))

    for directory in _COMMON_BIN_DIRS:
        base = Path(directory).expanduser()
        for name in names:
            candidates.append(base / name)

    for raw in _WINDOWS_GH_PATHS:
        expanded = os.path.expandvars(raw)
        if "%" in expanded:
            continue
        candidates.append(Path(expanded))

    return candidates


def resolve() -> str | None:
    """The ``gh`` program path, or ``None`` when it is not installed.

    Precedence: ``SLICEME_GH``, ``PATH``, then the common install directories.
    """
    for candidate in _candidate_paths():
        if _executable(candidate):
            return str(candidate)
    return None


def _missing_program() -> SlicemeError:
    return SlicemeError(
        "sliceme cannot find the `gh` program; install GitHub CLI to deliver "
        "with a pull request, or set SLICEME_GH to the program path"
    )


def available() -> bool:
    """Whether sliceme can find the ``gh`` program."""
    return resolve() is not None


def require() -> None:
    """Raise a clear error when sliceme cannot find the ``gh`` program."""
    if not available():
        raise _missing_program()


def _run(root: Path, args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    if not available():
        raise _missing_program()
    proc = subprocess.run(
        [resolve() or "gh", *args], cwd=str(root), capture_output=True, text=True
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
