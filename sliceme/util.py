"""Small dependency-free helpers shared across the local plane."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

STATE_DIR = ".sliceme"
CONFIG_NAME = "config.json"
DB_NAME = "state.db"
WORKTREES_DIR = "worktrees"
SCRATCH_DIR = "scratch"


class SlicemeError(Exception):
    """User-facing error.  The CLI prints the message and exits non-zero."""


def now() -> float:
    return time.time()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_text(json.dumps(obj, sort_keys=True, separators=(",", ":")))


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (slug or "unit")[:max_len]


def branch_key(branch: str) -> str:
    """The file prefix and registry key for a campaign branch.

    ``feat/x`` maps to ``feat--x``.  The mapping is lossy (``feat--x`` also
    maps here), so callers that create a campaign must reject a collision.
    """
    key = (branch or "main").strip().replace("/", "--")
    return key or "main"


def find_repo_root(start: str | os.PathLike[str] | None = None) -> Path:
    """Walk up from *start* to the nearest directory holding ``.sliceme``."""
    current = Path(start or os.getcwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / STATE_DIR / CONFIG_NAME).is_file():
            return candidate
    raise SlicemeError(
        "not inside an Sliceme workspace (run `sliceme init` first)"
    )


def state_dir(root: Path) -> Path:
    return root / STATE_DIR


def db_path(root: Path) -> Path:
    return state_dir(root) / DB_NAME


def config_path(root: Path) -> Path:
    return state_dir(root) / CONFIG_NAME


def worktrees_dir(root: Path) -> Path:
    return state_dir(root) / WORKTREES_DIR


def scratch_dir(root: Path) -> Path:
    return state_dir(root) / SCRATCH_DIR


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def rmtree(path: Path) -> None:
    import shutil

    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
