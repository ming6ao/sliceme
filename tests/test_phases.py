"""DAG phase planner (``sliceme/ownership.py``).

Pins the strict scheduling contract:
* phases are a deterministic projection of the DAG;
* per-phase size is capped by ``concurrency`` (default 3);
* ``depends_on`` forces a later phase;
* any owned-directory subtree overlap forces a later phase;
* ``owns`` may only name directories.
"""

import unittest

from sliceme.util import SlicemeError
from sliceme.ownership import (
    DEFAULT_PHASE_SIZE,
    plan_dag_phases,
    validate_dag,
)


def node(nid, owns=None, depends_on=None):
    return {"id": nid, "owns": owns or [], "depends_on": depends_on or []}


class PhasePlanTests(unittest.TestCase):
    def members(self, phases):
        return [w.members for w in phases]

    def test_independent_non_conflicting_nodes_share_a_phase(self):
        phases = plan_dag_phases(
            [
                node("w1", ["src/a"]),
                node("w2", ["src/b"]),
                node("w3", ["src/c"]),
            ]
        )
        self.assertEqual(self.members(phases), [["w1", "w2", "w3"]])

    def test_phase_size_caps_membership_and_opens_a_new_phase(self):
        nodes = [node(f"w{i}", [f"d{i}"]) for i in range(5)]
        phases = plan_dag_phases(nodes, phase_size=3)
        self.assertEqual(self.members(phases), [["w0", "w1", "w2"], ["w3", "w4"]])

    def test_default_phase_size_is_three(self):
        nodes = [node(f"w{i}", [f"d{i}"]) for i in range(4)]
        phases = plan_dag_phases(nodes)
        self.assertEqual(DEFAULT_PHASE_SIZE, 3)
        self.assertEqual([len(w.members) for w in phases], [3, 1])

    def test_dependency_forces_a_later_phase(self):
        phases = plan_dag_phases(
            [
                node("w2", ["src/b"], depends_on=["w1"]),
                node("w1", ["src/a"]),
            ]
        )
        self.assertEqual(self.members(phases), [["w1"], ["w2"]])

    def test_dependency_barrier_holds_even_when_phase_has_room(self):
        phases = plan_dag_phases(
            [
                node("w1", ["src/a"]),
                node("w2", ["src/b"], depends_on=["w1"]),
                node("w3", ["src/c"]),
            ]
        )
        # w3 may share w1's phase, but w2 cannot.
        self.assertEqual(self.members(phases), [["w1", "w3"], ["w2"]])

    def test_same_directory_conflicts(self):
        phases = plan_dag_phases(
            [
                node("w1", ["src/api"]),
                node("w2", ["src/api"]),
            ]
        )
        self.assertEqual(self.members(phases), [["w1"], ["w2"]])
        self.assertIn("w2", phases[1].conflicts)
        self.assertIn("w1", phases[1].conflicts["w2"])

    def test_ancestor_and_descendant_conflict(self):
        phases = plan_dag_phases(
            [
                node("w1", ["src"]),
                node("w2", ["src/api"]),
            ]
        )
        self.assertEqual(self.members(phases), [["w1"], ["w2"]])

    def test_root_owns_the_whole_tree(self):
        phases = plan_dag_phases(
            [
                node("w1", ["."]),
                node("w2", ["src/api"]),
            ]
        )
        self.assertEqual(self.members(phases), [["w1"], ["w2"]])

    def test_trailing_slash_and_prefix_normalize(self):
        phases = plan_dag_phases(
            [
                node("w1", ["src/api/"]),
                node("w2", ["src/api"]),
            ]
        )
        self.assertEqual(self.members(phases), [["w1"], ["w2"]])

    def test_sibling_directories_never_conflict(self):
        phases = plan_dag_phases(
            [
                node("w1", ["src/models"]),
                node("w2", ["src/model"]),
            ]
        )
        # The old token-similarity tier is gone: only subtree overlap matters.
        self.assertEqual(self.members(phases), [["w1", "w2"]])

    def test_nodes_without_owning_scopes_never_conflict(self):
        phases = plan_dag_phases([node("a"), node("b"), node("c")])
        self.assertEqual(self.members(phases), [["a", "b", "c"]])

    def test_dependency_is_min_phase_even_after_conflict(self):
        phases = plan_dag_phases(
            [
                node("w1", ["src/api"]),
                node("w2", ["src/api"]),
                node("w3", ["src/c"], depends_on=["w2"]),
            ]
        )
        # w2 conflicts with w1 -> phase 1; w3 must be strictly after w2.
        self.assertEqual(self.members(phases), [["w1"], ["w2"], ["w3"]])

    def test_planning_is_deterministic(self):
        nodes = [
            node("w2", ["src/b"], depends_on=["w1"]),
            node("w1", ["src/a"]),
            node("w3", ["src/a"]),
            node("w4", ["src/d"]),
        ]
        first = self.members(plan_dag_phases(nodes))
        second = self.members(plan_dag_phases(nodes))
        self.assertEqual(first, second)

    def test_unknown_dependency_raises(self):
        with self.assertRaises(SlicemeError):
            plan_dag_phases([node("w1", depends_on=["ghost"])])

    def test_cycle_raises(self):
        with self.assertRaises(SlicemeError):
            plan_dag_phases(
                [
                    node("w1", depends_on=["w2"]),
                    node("w2", depends_on=["w1"]),
                ]
            )

    def test_duplicate_id_raises(self):
        with self.assertRaises(SlicemeError):
            plan_dag_phases([node("w1"), node("w1")])

    def test_phase_size_must_be_positive(self):
        with self.assertRaises(SlicemeError):
            plan_dag_phases([node("w1")], phase_size=0)

    def test_non_directory_owns_is_rejected(self):
        with self.assertRaises(SlicemeError) as ctx:
            plan_dag_phases([node("w1", ["file:src/a.py"])])
        self.assertIn("must be a repository-relative directory", str(ctx.exception))

    def test_validate_dag_accepts_and_rejects(self):
        validate_dag([node("w1", ["src/api"]), node("w2", ["docs"])])
        with self.assertRaises(SlicemeError):
            validate_dag([node("w1", ["symbol:src/a.py#A"])])


class RemovedGpuFieldTests(unittest.TestCase):
    """Sliceme does not manage GPUs; a plan that sets ``gpu`` is rejected."""

    def test_a_gpu_field_is_rejected(self):
        dag = [{**node("w1", ["src/a"]), "gpu": "T1"}]
        with self.assertRaises(SlicemeError) as ctx:
            validate_dag(dag)
        self.assertIn("gpu", str(ctx.exception))

    def test_a_gpu_field_is_rejected_before_the_phase_plan(self):
        dag = [{**node("w1", ["src/a"]), "gpu": "none"}]
        with self.assertRaises(SlicemeError):
            plan_dag_phases(dag)


class ReadinessTests(unittest.TestCase):
    """The readiness filter is retired; the phase is the one scheduling gate.

    See ``tests/test_phase_cursor.py``: one cursor drives ``ready``, ``record``,
    and ``check``, and a recorded node satisfies a dependency.
    """

    def test_recorded_node_satisfies_a_dependency(self):
        nodes = [node("a"), node("b", depends_on=["a"])]
        # A dependency lives in an earlier phase, so recording ``a`` advances the
        # phase cursor and makes ``b`` ready; there is no dependency filter.
        phases = plan_dag_phases(nodes)
        phase_of = {m: w.index for w in phases for m in w.members}
        self.assertLess(phase_of["a"], phase_of["b"])


if __name__ == "__main__":
    unittest.main()
