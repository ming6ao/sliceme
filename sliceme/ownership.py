"""Plan-time directory ownership and the DAG phase projection.

Ownership is by **directory subtree**. A campaign node declares exactly one
repo-relative directory in ``dir`` (or in a one-entry ``owns`` list, the
migration shim). A node names the deepest directory that contains each path it
will add, modify, or delete. Files, symbols, APIs, and operations do not
participate: the planner is the single author of ownership, and the phase
projection here serializes overlapping nodes.

Phases are the **scheduler** projection of ``dag.json``. The DAG remains the
only authored schedule; a phase is a maximal set of nodes that may run
concurrently (each in its own worktree) and do not conflict on any owned
directory. A node is never placed earlier than ``max(phase(dep) + 1)``, so every
dependency is integrated before its dependents start. The per-phase size is
capped by ``concurrency`` (default 3).

One directory appears in exactly one node. The planner splits a multi-directory
node, and it merges two visits of one directory. The engine also merges nodes
that share one directory, because that merge is mechanical. The engine returns
``revisited_directory`` when the merge would make a cycle, or when a
``no_merge`` node shares its directory with another node.

A node must not set the removed ``gpu`` field. Sliceme does not manage GPUs, so
the engine rejects a plan that sets it.

Canonical ownership form:

* ``src/api`` and ``src/api/`` both normalize to ``src/api``;
* the repository root is ``"."``;
* a kind prefix (``dir:``, ``file:``, ``symbol:``, ...) is rejected, so a plan
  carries repository-relative directories only and can never silently rely on
  finer-grained enforcement that no longer exists.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from .util import SlicemeError

DEFAULT_PHASE_SIZE = 3

#: A node id is a slug. It is also the worker log suffix, so it never contains
#: a path separator.
NODE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _plan_error(reason: str, message: str) -> SlicemeError:
    """A plan error carrying a machine-readable reason code.

    The code is on both the ``reason`` attribute and at the head of the
    message, so a caller can switch on the code and never parse the text.
    """
    error = SlicemeError(f"{reason}: {message}")
    error.reason = reason
    return error


# ---------------------------------------------------------------------------
# Directory ownership
# ---------------------------------------------------------------------------
def normalize_dir(path: str) -> str:
    """Canonical repo-relative directory: ``src/api/`` -> ``src/api``."""
    text = str(path).strip().replace("\\", "/")
    norm = posixpath.normpath(text)
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.strip("/")
    return norm or "."


def parse_dir(spec: Any) -> str:
    """Parse the ``dir`` field into one normalized repository directory.

    An empty or whitespace-only value and a kind prefix (``dir:``, ``file:``,
    ``symbol:``, ...) each raise :class:`SlicemeError`.
    """
    text = str(spec).strip()
    if not text:
        raise SlicemeError(
            "dir entry is empty; declare the one directory the node touches"
        )
    if ":" in text:
        raise SlicemeError(
            f"dir must be a repository-relative directory, not '{text}' "
            "(write the bare path, for example 'src/api')"
        )
    return normalize_dir(text)


def parse_owns(specs: list[str]) -> list[str]:
    """Parse an ``owns`` list into normalized repository-relative directories.

    Accepts bare directory paths only. An empty or whitespace-only entry and a
    kind prefix (``dir:``, ``file:``, ``symbol:``, ...) each raise
    :class:`SlicemeError`. A missing ``owns`` list stays valid (an empty list
    parses to ``[]``).
    """
    owns: list[str] = []
    seen: set[str] = set()
    for spec in specs:
        text = str(spec).strip()
        if not text:
            raise SlicemeError(
                "owns entry is empty; declare the deepest directory the node touches"
            )
        if ":" in text:
            raise SlicemeError(
                f"owns must be a repository-relative directory, not '{text}' "
                "(write the bare path, for example 'src/api')"
            )
        directory = normalize_dir(text)
        if directory not in seen:
            seen.add(directory)
            owns.append(directory)
    return owns


def owns_conflict(a: list[str], b: list[str]) -> str | None:
    """Return a human reason when two owned directory sets overlap.

    Overlap is subtree overlap: equal directories, or one being an ancestor of
    the other. ``None`` means the two sets may run in the same phase.
    """
    dirs_a = {normalize_dir(x) for x in a}
    dirs_b = {normalize_dir(y) for y in b}
    for x in sorted(dirs_a):
        for y in sorted(dirs_b):
            if x == y:
                return f"directory conflict on {x}"
            if x == "." or y == ".":
                return f"directory conflict: root contains {y if x == '.' else x}"
            if x.startswith(y + "/"):
                return f"directory conflict: {y} contains {x}"
            if y.startswith(x + "/"):
                return f"directory conflict: {x} contains {y}"
    return None


def _parent_dir(path: Any) -> str:
    """The directory that contains *path*, normalized."""
    return posixpath.dirname(str(path).replace("\\", "/")).strip("/") or "."


def path_within_owns(path: str, owns: list[str]) -> bool:
    """True when *path* (a changed file) lives in one of the owned directories."""
    parent = _parent_dir(path)
    for owned in owns:
        directory = normalize_dir(owned)
        if directory == "." or parent == directory or parent.startswith(directory + "/"):
            return True
    return False


def deepest_owner_of(path: str, dirs: dict[str, str | None]) -> str | None:
    """The node id whose directory is the deepest match for *path*, or ``None``.

    The one-visit rule gives each directory one node, so the deepest match is
    unambiguous. A root node (``"."``) is the shallowest match, so a deeper
    node always wins.
    """
    parent = _parent_dir(path)
    best_id: str | None = None
    best_depth = -1
    for node_id in sorted(dirs):
        directory = dirs[node_id]
        if not directory:
            continue
        directory = normalize_dir(directory)
        if directory != "." and parent != directory and not parent.startswith(directory + "/"):
            continue
        depth = 0 if directory == "." else directory.count("/") + 1
        if depth > best_depth:
            best_depth = depth
            best_id = str(node_id)
    return best_id


# ---------------------------------------------------------------------------
# DAG phase projection
# ---------------------------------------------------------------------------
@dataclass
class DagPhase:
    index: int
    members: list[str] = field(default_factory=list)
    conflicts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.index,
            "members": list(self.members),
            "conflicts": dict(self.conflicts),
        }


def _node_id(node: dict[str, Any]) -> str:
    nid = node.get("id")
    if not nid:
        raise SlicemeError("dag node is missing an id")
    return str(nid)


def _check_node_id(node: dict[str, Any]) -> str:
    """Validate and return a node id.

    The id is the worker log suffix, so it must be a slug. A path id would
    create a missing log directory.
    """
    nid = _node_id(node)
    if not NODE_ID_RE.match(nid):
        raise _plan_error(
            "bad_node_id",
            f"dag node id '{nid}' must match [A-Za-z0-9._-]{{1,128}}; "
            "do not use a directory path as the node id",
        )
    return nid


def _reject_removed_fields(node: dict[str, Any]) -> None:
    """Reject a node field that Sliceme no longer owns (``gpu``)."""
    if "gpu" in node:
        raise SlicemeError(
            f"dag node '{_node_id(node)}' sets the removed 'gpu' field; "
            "Sliceme does not manage GPUs"
        )


def node_dir(node: dict[str, Any]) -> str | None:
    """Parse a node's one directory from ``dir`` or the one-entry ``owns`` shim.

    Raises :class:`SlicemeError` with reason ``multi_directory_node`` when
    ``owns`` holds two or more entries, and ``conflicting_directory`` when
    ``dir`` and ``owns`` disagree. Returns ``None`` when the node declares no
    directory (a node that changes no file).
    """
    nid = str(node.get("id") or "?")
    has_dir = "dir" in node
    owns_raw = node.get("owns")
    owns: list[str] = []
    if owns_raw:
        if not isinstance(owns_raw, (list, tuple)):
            raise SlicemeError(f"dag node '{nid}' owns must be a list")
        owns = parse_owns([str(entry) for entry in owns_raw])
        if len(owns) > 1:
            raise _plan_error(
                "multi_directory_node",
                f"dag node '{nid}' declares {len(owns)} directories "
                f"({', '.join(owns)}); declare exactly one",
            )
    if has_dir:
        directory = parse_dir(node.get("dir"))
        if owns and owns[0] != directory:
            raise _plan_error(
                "conflicting_directory",
                f"dag node '{nid}' sets dir '{directory}' and owns '{owns[0]}'; "
                "use one value",
            )
        return directory
    if owns:
        return owns[0]
    return None


def node_owns(node: dict[str, Any]) -> list[str]:
    """Parse a node's one directory as a list (``dir`` or the ``owns`` shim)."""
    directory = node_dir(node)
    return [directory] if directory else []


def _conflict_reason(a: dict[str, Any], b: dict[str, Any]) -> str | None:
    """Return a human reason when nodes *a* and *b* must not share a phase.

    When *a* is compared against *b*, *b* is already placed and *a* is the
    record moving later, so the message names the blocker (*b*). The conflict
    is the strict subtree conflict on the owned directories.
    """
    owns_a = node_owns(a)
    if not owns_a:
        return None
    reason = owns_conflict(owns_a, node_owns(b))
    if reason:
        return f"{reason} with '{_node_id(b)}'"
    return None


def _topological_order(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Kahn topological sort, stable by declaration order.

    Raises :class:`SlicemeError` on duplicate ids, unknown dependencies, or a
    dependency cycle.
    """
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for node in nodes:
        _reject_removed_fields(node)
        nid = _check_node_id(node)
        if nid in by_id:
            raise _plan_error("duplicate_node", f"dag node id '{nid}' appears more than once")
        by_id[nid] = node
        order.append(nid)

    indegree: dict[str, int] = {nid: 0 for nid in order}
    dependents: dict[str, list[str]] = {nid: [] for nid in order}
    for nid in order:
        node = by_id[nid]
        for dep in node.get("depends_on") or []:
            dep = str(dep)
            if dep not in by_id:
                raise _plan_error(
                    "unknown_dependency",
                    f"dag node '{nid}' depends on unknown node '{dep}'",
                )
            indegree[nid] += 1
            dependents[dep].append(nid)

    # Always take the earliest-declared zero-indegree node so placement is
    # deterministic and independent of dict/set iteration order.
    placed: set[str] = set()
    sorted_ids: list[str] = []
    while len(sorted_ids) < len(order):
        picked = next(
            (nid for nid in order if nid not in placed and indegree[nid] == 0), None
        )
        if picked is None:
            remaining = [nid for nid in order if nid not in placed]
            raise _plan_error(
                "dependency_cycle",
                f"dependency cycle among dag nodes: {', '.join(remaining)}",
            )
        placed.add(picked)
        sorted_ids.append(picked)
        for dependent in dependents[picked]:
            indegree[dependent] -= 1

    return [by_id[nid] for nid in sorted_ids]


def validate_dag(nodes: list[dict[str, Any]]) -> None:
    """Validate a DAG's ids, edges, acyclicity, and one-directory rule.

    One directory appears in exactly one node. Raises :class:`SlicemeError` with
    reason ``revisited_directory`` when a directory appears twice. Callers that
    want the defensive merge run :func:`merge_same_own_nodes` first.
    """
    ordered = _topological_order(nodes)  # raises on duplicates/unknown/cycle
    seen: dict[str, str] = {}
    for node in ordered:
        directory = node_dir(node)  # raises on a multi-directory node
        if directory is None:
            continue
        if directory in seen:
            raise _plan_error(
                "revisited_directory",
                f"directory '{directory}' appears in nodes "
                f"'{seen[directory]}' and '{_node_id(node)}'",
            )
        seen[directory] = _node_id(node)


def plan_dag_phases(
    nodes: list[dict[str, Any]],
    *,
    phase_size: int = DEFAULT_PHASE_SIZE,
) -> list[DagPhase]:
    """Pack DAG *nodes* into ordered phases.

    ``phase_size`` is the maximum number of nodes per phase (the campaign's
    ``concurrency``, default 3).  A node is placed in the earliest phase that

    * is at least ``max(phase(dep) + 1)`` for every dependency,
    * has room under ``phase_size``, and
    * contains no node whose owned directories overlap (strict subtree rule).
    """
    if phase_size < 1:
        raise SlicemeError("phase_size (concurrency) must be >= 1")

    phases: list[DagPhase] = []
    phase_of: dict[str, int] = {}
    by_id = {_node_id(n): n for n in nodes}

    for node in _topological_order(nodes):
        nid = _node_id(node)
        node_owns(node)  # validate directory-only owns, even for lone nodes
        min_phase = 0
        for dep in node.get("depends_on") or []:
            min_phase = max(min_phase, phase_of[str(dep)] + 1)

        blocked_reason: str | None = None
        placed = False
        for phase in phases:
            if phase.index < min_phase:
                continue
            if len(phase.members) >= phase_size:
                continue
            reason: str | None = None
            for member_id in phase.members:
                reason = _conflict_reason(node, by_id[member_id])
                if reason:
                    break
            if reason:
                blocked_reason = reason
                continue
            phase.members.append(nid)
            phase_of[nid] = phase.index
            placed = True
            break

        if not placed:
            phase = DagPhase(index=len(phases))
            phase.members.append(nid)
            if blocked_reason:
                phase.conflicts[nid] = blocked_reason
            phases.append(phase)
            phase_of[nid] = phase.index

    return phases


# ---------------------------------------------------------------------------
# DAG normalization: contract one directory into one node
# ---------------------------------------------------------------------------
def _merged_node(by_id: dict[str, dict[str, Any]], members: list[str]) -> dict[str, Any]:
    """Assemble one survivor node from an ordered, non-empty group of ids."""
    survivor = members[0]
    if len(members) == 1:
        return dict(by_id[survivor])
    sources = [by_id[member] for member in members]
    directory = node_dir(by_id[survivor])
    merged: dict[str, Any] = {
        **by_id[survivor],
        "id": survivor,
        "label": " + ".join(
            str(source.get("label") or _node_id(source)) for source in sources
        ),
        "goal": "\n\n".join(
            f"[{_node_id(source)}] {str(source.get('goal') or '').strip()}".strip()
            for source in sources
        ),
        "depends_on": list(
            dict.fromkeys(
                str(dep)
                for source in sources
                for dep in (source.get("depends_on") or [])
                if str(dep) not in members
            )
        ),
        "acceptance": list(
            dict.fromkeys(
                str(command)
                for source in sources
                for command in (source.get("acceptance") or [])
            )
        ),
        "merged_from": members[1:],
    }
    # The canonical field is ``dir``; drop the migration shim from the survivor.
    merged.pop("owns", None)
    if directory is not None:
        merged["dir"] = directory
    tasks = list(
        dict.fromkeys(
            str(task)
            for source in sources
            for task in (source.get("tasks") or [])
        )
    )
    if tasks:
        merged["tasks"] = tasks
    return merged


def merge_same_own_nodes(
    nodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Contract nodes that share one directory into one node.

    One directory appears in exactly one node, so every group of nodes with the
    same directory merges into the lowest topological survivor.  Returns the
    merged nodes and a map from every absorbed id to its survivor.  The result
    is idempotent.

    Raises :class:`SlicemeError` with reason ``revisited_directory`` when the
    merge would make a cycle, or when a ``no_merge`` node shares its directory
    with another node.  A node with recorded progress gets ``no_merge`` at
    projection time, so a defensive merge never rewrites a recorded node.
    """
    ordered = _topological_order(nodes)  # validates ids, deps, and cycles
    by_id = {_node_id(node): node for node in ordered}
    order = {_node_id(node): index for index, node in enumerate(ordered)}

    groups: dict[str, list[str]] = {}
    for nid in by_id:
        directory = node_dir(by_id[nid])
        if directory is not None:
            groups.setdefault(directory, []).append(nid)

    parent = {nid: nid for nid in by_id}

    def root(nid: str) -> str:
        while parent[nid] != nid:
            nid = parent[nid]
        return nid

    for directory, members in groups.items():
        if len(members) < 2:
            continue
        protected = [member for member in members if by_id[member].get("no_merge")]
        if protected:
            raise _plan_error(
                "revisited_directory",
                f"directory '{directory}' appears in nodes {members}, and "
                f"node(s) {protected} set no_merge; the engine cannot merge them",
            )
        for member in members[1:]:
            parent[root(member)] = root(members[0])

    grouped: dict[str, list[str]] = {}
    for nid in by_id:
        grouped.setdefault(root(nid), []).append(nid)

    merge_map: dict[str, str] = {}
    survivors: dict[str, dict[str, Any]] = {}
    for members in grouped.values():
        members.sort(key=order.__getitem__)
        merge_map.update(dict.fromkeys(members[1:], members[0]))
        survivors[members[0]] = _merged_node(by_id, members)

    # Rewrite every dependency that points at an absorbed node.
    for node in survivors.values():
        deps = dict.fromkeys(
            merge_map.get(str(dep), str(dep)) for dep in node.get("depends_on") or []
        )
        node["depends_on"] = [dep for dep in deps if dep != node["id"]]

    merged = [survivors[root] for root in sorted(survivors, key=order.__getitem__)]
    try:
        _topological_order(merged)
    except SlicemeError as error:
        raise _plan_error(
            "revisited_directory",
            f"merging one directory into one node made a cycle: {error}",
        ) from error
    return merged, merge_map


__all__ = [
    "DEFAULT_PHASE_SIZE",
    "NODE_ID_RE",
    "DagPhase",
    "deepest_owner_of",
    "merge_same_own_nodes",
    "node_dir",
    "node_owns",
    "normalize_dir",
    "owns_conflict",
    "parse_dir",
    "parse_owns",
    "path_within_owns",
    "plan_dag_phases",
    "validate_dag",
]
