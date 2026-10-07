"""Plan-time directory ownership and the DAG wave projection.

Ownership is by **directory subtree**. A campaign node declares the deepest
repo-relative directories that contain the paths it will touch; two nodes
conflict when their owned directories overlap by subtree (equal, ancestor, or
descendant). Files, symbols, APIs, and operations do not participate: the
planner is the single author of ownership, and the wave projection here
serializes overlapping nodes.

Waves are the **scheduler** projection of ``dag.json``. The DAG remains the
only authored schedule; a wave is a maximal set of nodes that may run
concurrently (each in its own worktree) and do not conflict on any owned
directory. A node is never placed earlier than ``max(wave(dep) + 1)``, so every
dependency is integrated before its dependents start. The per-wave size is
capped by ``concurrency`` (default 3).

Canonical ownership form:

* ``dir:src/api`` and ``src/api/`` both normalize to ``src/api``;
* the repository root is ``"."``;
* non-directory specs (``file:``, ``symbol:``, ...) are rejected, so a plan can
  never silently rely on finer-grained enforcement that no longer exists.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any

from .util import SlicemeError

DEFAULT_WAVE_SIZE = 3

#: Scope kinds that are *not* ownable. Kept explicit so a stale plan fails
#: loudly instead of being reinterpreted as a directory path.
_NON_DIR_KINDS = frozenset(
    {"file", "symbol", "api", "schema", "config", "migration", "infra", "test", "unknown"}
)


# ---------------------------------------------------------------------------
# Directory ownership
# ---------------------------------------------------------------------------
def normalize_dir(path: str) -> str:
    """Canonical repo-relative directory: ``dir:src/api/`` -> ``src/api``."""
    text = str(path).strip()
    if ":" in text:
        prefix, rest = text.split(":", 1)
        if prefix.strip().lower() == "dir":
            text = rest
    text = text.replace("\\", "/")
    norm = posixpath.normpath(text)
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.strip("/")
    return norm or "."


def parse_owns(specs: list[str]) -> list[str]:
    """Parse an ``owns`` list into normalized directories.

    Accepts ``dir:path`` and bare paths. An empty or whitespace-only entry and
    a recognized non-directory kind each raise :class:`SlicemeError`. A missing
    ``owns`` list stays valid (an empty list parses to ``[]``).
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
            prefix = text.split(":", 1)[0].strip().lower()
            if prefix in _NON_DIR_KINDS:
                raise SlicemeError(
                    f"owns must be directories, not {prefix}: '{text}' "
                    "(declare the deepest directory that contains the paths)"
                )
        directory = normalize_dir(text)
        if directory not in seen:
            seen.add(directory)
            owns.append(directory)
    return owns


def owns_conflict(a: list[str], b: list[str]) -> str | None:
    """Return a human reason when two owned directory sets overlap.

    Overlap is subtree overlap: equal directories, or one being an ancestor of
    the other. ``None`` means the two sets may run in the same wave.
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


def path_within_owns(path: str, owns: list[str]) -> bool:
    """True when *path* (a changed file) lives in one of the owned directories."""
    parent = posixpath.dirname(str(path).replace("\\", "/")).strip("/") or "."
    for owned in owns:
        directory = normalize_dir(owned)
        if directory == "." or parent == directory or parent.startswith(directory + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# DAG wave projection
# ---------------------------------------------------------------------------
@dataclass
class DagWave:
    index: int
    members: list[str] = field(default_factory=list)
    conflicts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wave": self.index,
            "members": list(self.members),
            "conflicts": dict(self.conflicts),
        }


def _node_id(node: dict[str, Any]) -> str:
    nid = node.get("id")
    if not nid:
        raise SlicemeError("dag node is missing an id")
    return str(nid)


def node_owns(node: dict[str, Any]) -> list[str]:
    """Parse a node's ``owns`` into normalized directories (validates kinds)."""
    return parse_owns(node.get("owns") or [])


def _conflict_reason(a: dict[str, Any], b: dict[str, Any]) -> str | None:
    """Return a human reason when nodes *a* and *b* must not share a wave.

    When *a* is compared against *b*, *b* is already placed and *a* is the
    candidate moving later, so the message names the blocker (*b*).
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
        nid = _node_id(node)
        if nid in by_id:
            raise SlicemeError(f"dag node id '{nid}' appears more than once")
        by_id[nid] = node
        order.append(nid)

    indegree: dict[str, int] = {nid: 0 for nid in order}
    dependents: dict[str, list[str]] = {nid: [] for nid in order}
    for nid in order:
        node = by_id[nid]
        for dep in node.get("depends_on") or []:
            dep = str(dep)
            if dep not in by_id:
                raise SlicemeError(f"dag node '{nid}' depends on unknown node '{dep}'")
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
            raise SlicemeError(
                f"dependency cycle among dag nodes: {', '.join(remaining)}"
            )
        placed.add(picked)
        sorted_ids.append(picked)
        for dependent in dependents[picked]:
            indegree[dependent] -= 1

    return [by_id[nid] for nid in sorted_ids]


def validate_dag(nodes: list[dict[str, Any]]) -> None:
    """Validate a DAG's ids, edges, acyclicity, and directory-only ``owns``."""
    ordered = _topological_order(nodes)  # raises on duplicates/unknown/cycle
    for node in ordered:
        node_owns(node)  # raises on a non-directory owns spec


def plan_dag_waves(
    nodes: list[dict[str, Any]],
    *,
    wave_size: int = DEFAULT_WAVE_SIZE,
) -> list[DagWave]:
    """Pack DAG *nodes* into ordered waves.

    ``wave_size`` is the maximum number of nodes per wave (the campaign's
    ``concurrency``, default 3).  A node is placed in the earliest wave that

    * is at least ``max(wave(dep) + 1)`` for every dependency,
    * has room under ``wave_size``, and
    * contains no node whose owned directories overlap (strict subtree rule).
    """
    if wave_size < 1:
        raise SlicemeError("wave_size (concurrency) must be >= 1")

    waves: list[DagWave] = []
    wave_of: dict[str, int] = {}
    by_id = {_node_id(n): n for n in nodes}

    for node in _topological_order(nodes):
        nid = _node_id(node)
        node_owns(node)  # validate directory-only owns, even for lone nodes
        min_wave = 0
        for dep in node.get("depends_on") or []:
            min_wave = max(min_wave, wave_of[str(dep)] + 1)

        blocked_reason: str | None = None
        placed = False
        for wave in waves:
            if wave.index < min_wave:
                continue
            if len(wave.members) >= wave_size:
                continue
            reason: str | None = None
            for member_id in wave.members:
                reason = _conflict_reason(node, by_id[member_id])
                if reason:
                    break
            if reason:
                blocked_reason = reason
                continue
            wave.members.append(nid)
            wave_of[nid] = wave.index
            placed = True
            break

        if not placed:
            wave = DagWave(index=len(waves))
            wave.members.append(nid)
            if blocked_reason:
                wave.conflicts[nid] = blocked_reason
            waves.append(wave)
            wave_of[nid] = wave.index

    return waves


# ---------------------------------------------------------------------------
# Per-node readiness: the spawn gate (DEC-2)
# ---------------------------------------------------------------------------
#: Node statuses that mean a node is underway or finished, so it is never
#: "ready" to spawn again.
_NOT_READY_STATUSES = frozenset({"done", "running"})


def _status_of(state: dict[str, Any], node_id: str) -> str:
    """Read one node's status from a campaign state mapping."""
    entries = state.get("nodes") if isinstance(state, dict) else None
    entry = entries.get(node_id) if isinstance(entries, dict) else None
    if isinstance(entry, dict):
        return str(entry.get("status") or "pending")
    return "pending"


def readiness(nodes: list[dict[str, Any]], state: dict[str, Any]) -> list[str]:
    """Return the ids of the nodes ready to spawn, in DAG declaration order.

    A node is ready when its own status is neither ``done`` nor ``running`` and
    every id in its ``depends_on`` has status ``done``.  *state* is the campaign
    state mapping ``{"nodes": {id: {"status": ...}}}`` that
    :func:`sliceme.campaign.load_state` returns.  A dependency missing from
    *state* counts as pending, so the dependent is simply not ready yet.

    Readiness is the spawn gate (DEC-2).  Waves stay a display hint; a node
    starts once its dependencies complete rather than at a fixed wave.
    """
    done = {_node_id(n) for n in nodes if _status_of(state, _node_id(n)) == "done"}
    ready: list[str] = []
    for node in nodes:
        nid = _node_id(node)
        if _status_of(state, nid) in _NOT_READY_STATUSES:
            continue
        if all(str(dep) in done for dep in (node.get("depends_on") or [])):
            ready.append(nid)
    return ready


# ---------------------------------------------------------------------------
# DAG normalization: contract same-ownership chains
# ---------------------------------------------------------------------------
#: GPU tiers, weakest to strongest. A merged node needs the strongest tier.
_GPU_RANK = {"none": 0, "T1": 1, "T2": 2}


def _merged_node(by_id: dict[str, dict[str, Any]], members: list[str]) -> dict[str, Any]:
    """Assemble one survivor node from an ordered, non-empty group of ids."""
    survivor = members[0]
    if len(members) == 1:
        return dict(by_id[survivor])
    sources = [by_id[member] for member in members]
    return {
        **by_id[survivor],
        "id": survivor,
        "label": " + ".join(
            str(source.get("label") or _node_id(source)) for source in sources
        ),
        "goal": "\n\n".join(
            f"[{_node_id(source)}] {str(source.get('goal') or '').strip()}".strip()
            for source in sources
        ),
        "owns": [
            f"dir:{directory}"
            for directory in sorted({d for source in sources for d in node_owns(source)})
        ],
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
        "gpu": max(
            (str(source.get("gpu") or "none") for source in sources),
            key=lambda tier: _GPU_RANK.get(tier, 0),
        ),
        "merged_from": members[1:],
    }


def merge_same_own_nodes(
    nodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Contract same-ownership nodes that share a dependency edge.

    Two nodes merge when a ``depends_on`` edge connects them, their owned
    directories are equal, and neither sets ``no_merge``. ``phase`` is a
    display label only and never gates a merge.
    The lowest topological index survives.  Returns the merged nodes and a map
    from every absorbed id to its survivor.  The result is idempotent.
    """
    ordered = _topological_order(nodes)  # validates ids, deps, and cycles
    by_id = {_node_id(node): node for node in ordered}
    order = {_node_id(node): index for index, node in enumerate(ordered)}
    for node in ordered:
        node_owns(node)  # validate directory-only owns, even for lone nodes

    parent = {nid: nid for nid in by_id}

    def root(nid: str) -> str:
        while parent[nid] != nid:
            nid = parent[nid]
        return nid

    def mergeable(a: str, b: str) -> bool:
        node_a, node_b = by_id[a], by_id[b]
        if node_a.get("no_merge") or node_b.get("no_merge"):
            return False
        owns_a, owns_b = node_owns(node_a), node_owns(node_b)
        return bool(owns_a) and sorted(owns_a) == sorted(owns_b)

    # Union every compatible edge. The lowest topological index wins.
    for node in ordered:
        nid = _node_id(node)
        for dep in map(str, node.get("depends_on") or []):
            if dep in by_id and mergeable(nid, dep):
                left, right = root(nid), root(dep)
                if left != right:
                    if order[left] > order[right]:
                        left, right = right, left
                    parent[right] = left

    groups: dict[str, list[str]] = {}
    for nid in by_id:
        groups.setdefault(root(nid), []).append(nid)

    merge_map: dict[str, str] = {}
    survivors: dict[str, dict[str, Any]] = {}
    for members in groups.values():
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
    return merged, merge_map


__all__ = [
    "DEFAULT_WAVE_SIZE",
    "DagWave",
    "merge_same_own_nodes",
    "node_owns",
    "normalize_dir",
    "owns_conflict",
    "parse_owns",
    "path_within_owns",
    "plan_dag_waves",
    "readiness",
    "validate_dag",
]
