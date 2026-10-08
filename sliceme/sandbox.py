"""Sandbox profiles, project manifests, and the coordinator's sandbox gate.

Two layers:

* **Isolation profiles.**  A :class:`Sandbox` describes how a check command is
  isolated: a built-in mode (``none``/``bwrap``/``unshare``) or a project
  ``command`` prefix, plus network/read-only/writable policy and an optional
  GPU runner.  Its :meth:`Sandbox.digest` is folded into the verification
  fingerprint, so tightening isolation (or changing setup) invalidates a cached
  verdict.
* **Project manifests.**  The target repository provides *how to run tests in
  isolation* as a tracked manifest (``sliceme.sandbox.json``,
  ``.sliceme-sandbox.json``, or ``tools/sliceme-sandbox.json`` -- deliberately
  **not** under ``.sliceme/``, which is git-excluded).  The planner references
  it in ``dag.json``; the coordinator validates the gate before verifying.

Resolution precedence: explicit ``--sandbox`` override > ``dag.json.sandbox`` >
plane ``policy.sandbox`` > discovered project manifest > ``none``.  ``none`` is
unsandboxed; a campaign that sets ``policy.require_sandbox`` or
``dag.json.sandbox_required`` fails closed when no profile exists.

The GPU runner is **sliceme's**, not the target project's.  For a GPU check the
runner composes ``gpu runner -> sandbox -> acceptance``: the project may
override the runner through ``gpu.command`` in its manifest.
"""

from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .util import SlicemeError, read_json, sha256_json

#: Manifest filenames, searched in order at the repository root.
MANIFEST_NAMES = (
    "sliceme.sandbox.json",
    ".sliceme-sandbox.json",
    "tools/sliceme-sandbox.json",
)

#: The manifest schema version this engine understands.
MANIFEST_VERSION = 1

#: Built-in isolation modes.  ``none`` is unsandboxed and explicit.
SANDBOX_MODES = ("none", "bwrap", "unshare")

_BACKENDS = {"bwrap": "bwrap", "unshare": "unshare"}


@dataclass(frozen=True)
class Sandbox:
    """How a check command is isolated by the synchronous check runner."""

    mode: str = "none"
    network: bool = True
    readonly_repo: bool = True
    writable: tuple[str, ...] = ()
    #: Project command prefix; receives ``/bin/sh -lc <command>`` as final args.
    command: tuple[str, ...] = ()
    #: Commands run once per snapshot before acceptance.
    setup: tuple[str, ...] = ()
    #: GPU runner prefix with a ``{tier}`` placeholder.
    gpu_command: tuple[str, ...] = ()
    #: Tier -> timeout seconds.
    gpu_tiers: tuple[tuple[str, int], ...] = ()
    #: Manifest path relative to the repository root, when discovered.
    manifest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "network": self.network,
            "readonly_repo": self.readonly_repo,
            "writable": list(self.writable),
            "command": list(self.command),
            "setup": list(self.setup),
            "gpu_command": list(self.gpu_command),
            "gpu_tiers": {tier: int(seconds) for tier, seconds in self.gpu_tiers},
            "manifest": self.manifest,
        }

    @property
    def configured(self) -> bool:
        """True when a real isolation profile exists (not the ``none`` default)."""
        return bool(self.command) or self.mode != "none"

    @property
    def offline(self) -> bool:
        return not self.network

    def gpu_tier_timeout(self, tier: str, default: int = 3600) -> int:
        mapping = dict(self.gpu_tiers)
        return int(mapping.get(tier, default))

    def digest(self) -> str:
        """Hash the isolation semantics and setup; drives fingerprint invalidation."""
        return sha256_json(
            {
                "mode": self.mode,
                "network": self.network,
                "readonly_repo": self.readonly_repo,
                "writable": sorted(self.writable),
                "command": list(self.command),
                "setup": list(self.setup),
                "gpu_command": list(self.gpu_command),
                "gpu_tiers": {tier: int(seconds) for tier, seconds in sorted(self.gpu_tiers)},
            }
        )


def _tuple_of_str(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple)):
        raise SlicemeError(f"sandbox {field_name} must be a list of strings")
    return tuple(str(item) for item in value)


def coerce_sandbox(raw: Any) -> Sandbox:
    """Normalise a manifest/config value into a :class:`Sandbox`."""
    if raw is None:
        return Sandbox()
    if isinstance(raw, Sandbox):
        return raw
    if isinstance(raw, str):
        if raw not in SANDBOX_MODES:
            raise SlicemeError(
                f"unknown sandbox mode: {raw!r} (want one of {', '.join(SANDBOX_MODES)})"
            )
        return Sandbox(mode=raw)
    if isinstance(raw, dict):
        mode = str(raw.get("mode") or "none")
        if mode not in SANDBOX_MODES:
            raise SlicemeError(
                f"unknown sandbox mode: {mode!r} (want one of {', '.join(SANDBOX_MODES)})"
            )
        gpu = raw.get("gpu") or {}
        if not isinstance(gpu, dict):
            raise SlicemeError("sandbox gpu must be an object")
        gpu_command = _tuple_of_str(gpu.get("command") or raw.get("gpu_command"), "gpu.command")
        tiers = gpu.get("tiers") or {}
        if not isinstance(tiers, dict):
            raise SlicemeError("sandbox gpu.tiers must be an object")
        gpu_tiers = tuple(sorted((str(k), int(v)) for k, v in tiers.items()))
        writable = raw.get("writable") or ()
        if isinstance(writable, str):
            writable = (writable,)
        return Sandbox(
            mode=mode,
            network=bool(raw.get("network", True)),
            readonly_repo=bool(raw.get("readonly_repo", True)),
            writable=tuple(str(item) for item in writable),
            command=_tuple_of_str(raw.get("command"), "command"),
            setup=_tuple_of_str(raw.get("setup"), "setup"),
            gpu_command=gpu_command,
            gpu_tiers=gpu_tiers,
            manifest=str(raw["manifest"]) if raw.get("manifest") else None,
        )
    raise SlicemeError(f"invalid sandbox specification: {raw!r}")


# ---------------------------------------------------------------------------
# Project manifests
# ---------------------------------------------------------------------------
def find_manifest(root: Path) -> Path | None:
    for name in MANIFEST_NAMES:
        candidate = Path(root) / name
        if candidate.is_file():
            return candidate
    return None


def load_manifest(root: Path, path: Path) -> Sandbox:
    """Load and structurally validate a project sandbox manifest."""
    path = Path(path)
    if not path.is_file():
        raise SlicemeError(f"sandbox manifest not found: {path}")
    try:
        data = read_json(path)
    except (OSError, ValueError) as exc:
        raise SlicemeError(f"invalid sandbox manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SlicemeError(f"invalid sandbox manifest (expected a JSON object): {path}")
    version = int(data.get("version", MANIFEST_VERSION))
    if version != MANIFEST_VERSION:
        raise SlicemeError(
            f"unsupported sandbox manifest version {version} in {path} "
            f"(this engine understands {MANIFEST_VERSION})"
        )
    sandbox = coerce_sandbox(data)
    if not sandbox.configured:
        raise SlicemeError(
            f"sandbox manifest {path} must define a 'command' or a non-'none' 'mode'"
        )
    try:
        manifest = str(path.resolve().relative_to(Path(root).resolve()))
    except ValueError:
        manifest = str(path)
    return replace(sandbox, manifest=manifest)


def discover_sandbox(root: Path) -> Sandbox | None:
    path = find_manifest(Path(root))
    return load_manifest(Path(root), path) if path is not None else None


def resolve_sandbox(
    dag: dict[str, Any] | None,
    config: dict[str, Any] | None,
    *,
    root: Path | str | None = None,
    override: str | None = None,
) -> Sandbox:
    """Resolve the effective sandbox for a campaign (see module docstring)."""
    if override:
        return coerce_sandbox(override)

    dag_sandbox = (dag or {}).get("sandbox")
    if dag_sandbox is not None:
        if isinstance(dag_sandbox, dict) and dag_sandbox.get("path"):
            if root is None:
                raise SlicemeError("dag.json sandbox path requires a repository root")
            pointer = Path(str(dag_sandbox["path"]))
            manifest = pointer if pointer.is_absolute() else Path(root) / pointer
            sandbox = load_manifest(Path(root), manifest)
            expected = dag_sandbox.get("digest")
            if expected and sandbox.digest() != expected:
                raise SlicemeError(
                    f"sandbox manifest {pointer} changed since the plan "
                    "(digest mismatch); replan or restore the manifest"
                )
            return sandbox
        return coerce_sandbox(dag_sandbox)

    policy = (config or {}).get("policy") or {}
    if policy.get("sandbox") is not None:
        return coerce_sandbox(policy.get("sandbox"))

    if root is not None:
        discovered = discover_sandbox(Path(root))
        if discovered is not None:
            return discovered
    return Sandbox()


def is_required(dag: dict[str, Any] | None, config: dict[str, Any] | None) -> bool:
    policy = (config or {}).get("policy") or {}
    return bool(policy.get("require_sandbox")) or bool((dag or {}).get("sandbox_required"))


def default_gpu_runner() -> str | None:
    """The bundled sliceme GPU runner, resolved by package path (never cwd-relative)."""
    candidate = Path(__file__).resolve().parent.parent / "tools" / "gpu.sh"
    if candidate.is_file():
        return str(candidate)
    return shutil.which("sliceme-gpu")


# Deprecated alias kept for compatibility; read default_gpu_runner first.
default_gpu_broker = default_gpu_runner


def _validate_executable(token: str, root: Path | str | None, what: str) -> None:
    if not token:
        raise SlicemeError(f"{what} must not be empty")
    candidate = Path(token)
    if candidate.is_absolute():
        if not candidate.exists():
            raise SlicemeError(f"{what} '{token}' does not exist")
        return
    if root is not None and (Path(root) / candidate).exists():
        return
    if shutil.which(token) is None:
        raise SlicemeError(
            f"{what} '{token}' not found (neither in the repository nor on PATH)"
        )


def validate_sandbox(
    sandbox: Sandbox, root: Path | str | None = None, *, gpu_required: bool = False
) -> None:
    """Validate a resolved profile (command resolvable, GPU runner present).

    Built-in backends are *not* required to be installed here; ``wrap_command``
    fails closed at run time if the binary is missing.
    """
    if sandbox.command:
        _validate_executable(sandbox.command[0], root, "sandbox command")
    if gpu_required and not sandbox.gpu_command and not default_gpu_runner():
        raise SlicemeError(
            "a GPU job was requested but no GPU runner is configured; add "
            "gpu.command to the sandbox manifest or install the sliceme GPU runner"
        )


def require_sandbox(
    dag: dict[str, Any] | None,
    config: dict[str, Any] | None,
    *,
    root: Path | str | None = None,
    gpu_required: bool = False,
    override: str | None = None,
) -> Sandbox:
    """Resolve and validate the gate; fail closed when a sandbox is required."""
    sandbox = resolve_sandbox(dag, config, root=root, override=override)
    if is_required(dag, config) and not sandbox.configured:
        raise SlicemeError(
            "project sandbox not configured: add sliceme.sandbox.json (or set "
            "policy.sandbox); refusing to verify"
        )
    validate_sandbox(sandbox, root, gpu_required=gpu_required)
    return sandbox


# ---------------------------------------------------------------------------
# Command wrapping
# ---------------------------------------------------------------------------
def backend_available(mode: str) -> bool:
    if mode == "none":
        return True
    binary = _BACKENDS.get(mode)
    return bool(binary) and shutil.which(binary) is not None


def wrap_command(
    command: str,
    sandbox: Sandbox,
    *,
    worktree: str | None = None,
    tier: str = "none",
) -> str:
    """Wrap a shell *command* in the sandbox (and GPU runner) prefix.

    ``none`` returns the command unchanged.  A requested backend that is not
    installed is a hard error (fail closed), never a silent downgrade.
    """
    inner = _wrap_isolation(command, sandbox, worktree)
    if tier and tier != "none":
        inner = _wrap_gpu(inner, sandbox, tier)
    return inner


def _wrap_isolation(command: str, sandbox: Sandbox, worktree: str | None) -> str:
    if sandbox.command:
        parts = [*sandbox.command, "/bin/sh", "-lc", command]
        return " ".join(shlex.quote(part) for part in parts)
    if sandbox.mode == "none":
        return command
    if not backend_available(sandbox.mode):
        raise SlicemeError(
            f"sandbox mode '{sandbox.mode}' requested but '{_BACKENDS[sandbox.mode]}' "
            "is not installed"
        )
    if sandbox.mode == "bwrap":
        return _wrap_bwrap(command, sandbox, worktree)
    return _wrap_unshare(command, sandbox)


def _wrap_gpu(inner: str, sandbox: Sandbox, tier: str) -> str:
    # The GPU runner runs *outside* the sandbox: it needs the host lock and
    # nvidia-smi, then hands the sandbox-wrapped command to the device.
    prefix = list(sandbox.gpu_command)
    if not prefix:
        runner = default_gpu_runner()
        if not runner:
            raise SlicemeError(
                "GPU job requested but no GPU runner is configured; add "
                "gpu.command to the sandbox manifest"
            )
        prefix = [runner, "--tier", "{tier}", "--"]
    prefix = [part.replace("{tier}", tier) for part in prefix]
    parts = [*prefix, "/bin/sh", "-lc", inner]
    return " ".join(shlex.quote(part) for part in parts)


def _wrap_bwrap(command: str, sandbox: Sandbox, worktree: str | None) -> str:
    parts = ["bwrap", "--die-with-parent", "--unshare-pid"]
    if sandbox.offline:
        parts.append("--unshare-net")
    # The host is read-only unless the project explicitly allows writes, but the
    # scratch worktree stays writable so builds and test artifacts work.
    host_bind = "--ro-bind" if sandbox.readonly_repo else "--dev-bind"
    parts += [host_bind, "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    if worktree:
        parts += ["--bind", worktree, worktree]
    for path in sandbox.writable:
        target = path
        if not path.startswith("/") and worktree:
            target = f"{worktree.rstrip('/')}/{path}"
        parts += ["--bind", target, target]
    if worktree:
        parts += ["--chdir", worktree]
    parts += ["--", "/bin/sh", "-lc", command]
    return " ".join(shlex.quote(part) for part in parts)


def _wrap_unshare(command: str, sandbox: Sandbox) -> str:
    # ``unshare`` isolates namespaces but not the filesystem; the detached
    # scratch worktree remains the actual write barrier.  Projects that need a
    # stronger boundary provide their own ``command`` in the manifest.
    parts = ["unshare", "--mount", "--pid", "--fork", "--kill-child"]
    if sandbox.offline:
        parts.append("--net")
    parts += ["--", "/bin/sh", "-lc", command]
    return " ".join(shlex.quote(part) for part in parts)


__all__ = [
    "MANIFEST_NAMES",
    "MANIFEST_VERSION",
    "SANDBOX_MODES",
    "Sandbox",
    "backend_available",
    "coerce_sandbox",
    "default_gpu_broker",
    "default_gpu_runner",
    "discover_sandbox",
    "find_manifest",
    "is_required",
    "load_manifest",
    "require_sandbox",
    "resolve_sandbox",
    "validate_sandbox",
    "wrap_command",
]
