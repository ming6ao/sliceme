"""DAG wave planner (``sliceme/ownership.py``).

Pins the strict scheduling contract:
* waves are a deterministic projection of the DAG;
* per-wave size is capped by ``concurrency`` (default 3);
* ``depends_on`` forces a later wave;
* any owned-directory subtree overlap forces a later wave;
* ``owns`` may only name directories.
"""

import unittest

from sliceme.util import SlicemeError
from sliceme.ownership import (
    DEFAULT_WAVE_SIZE,
    plan_dag_waves,
    readiness,
    validate_dag,
)


def node(nid, owns=None, depends_on=None):
    return {"id": nid, "owns": owns or [], "depends_on": depends_on or []}


def gpu_node(nid, gpu, owns=None, depends_on=None):
    return {**node(nid, owns, depends_on), "gpu": gpu}


class WavePlanTests(unittest.TestCase):
    def members(self, waves):
        return [w.members for w in waves]

    def test_independent_non_conflicting_nodes_share_a_wave(self):
        waves = plan_dag_waves(
            [
                node("w1", ["dir:src/a"]),
                node("w2", ["dir:src/b"]),
                node("w3", ["dir:src/c"]),
            ]
        )
        self.assertEqual(self.members(waves), [["w1", "w2", "w3"]])

    def test_wave_size_caps_membership_and_opens_a_new_wave(self):
        nodes = [node(f"w{i}", [f"dir:d{i}"]) for i in range(5)]
        waves = plan_dag_waves(nodes, wave_size=3)
        self.assertEqual(self.members(waves), [["w0", "w1", "w2"], ["w3", "w4"]])

    def test_default_wave_size_is_three(self):
        nodes = [node(f"w{i}", [f"dir:d{i}"]) for i in range(4)]
        waves = plan_dag_waves(nodes)
        self.assertEqual(DEFAULT_WAVE_SIZE, 3)
        self.assertEqual([len(w.members) for w in waves], [3, 1])

    def test_dependency_forces_a_later_wave(self):
        waves = plan_dag_waves(
            [
                node("w2", ["dir:src/b"], depends_on=["w1"]),
                node("w1", ["dir:src/a"]),
            ]
        )
        self.assertEqual(self.members(waves), [["w1"], ["w2"]])

    def test_dependency_barrier_holds_even_when_wave_has_room(self):
        waves = plan_dag_waves(
            [
                node("w1", ["dir:src/a"]),
                node("w2", ["dir:src/b"], depends_on=["w1"]),
                node("w3", ["dir:src/c"]),
            ]
        )
        # w3 may share w1's wave, but w2 cannot.
        self.assertEqual(self.members(waves), [["w1", "w3"], ["w2"]])

    def test_same_directory_conflicts(self):
        waves = plan_dag_waves(
            [
                node("w1", ["dir:src/api"]),
                node("w2", ["dir:src/api"]),
            ]
        )
        self.assertEqual(self.members(waves), [["w1"], ["w2"]])
        self.assertIn("w2", waves[1].conflicts)
        self.assertIn("w1", waves[1].conflicts["w2"])

    def test_ancestor_and_descendant_conflict(self):
        waves = plan_dag_waves(
            [
                node("w1", ["dir:src"]),
                node("w2", ["dir:src/api"]),
            ]
        )
        self.assertEqual(self.members(waves), [["w1"], ["w2"]])

    def test_root_owns_the_whole_tree(self):
        waves = plan_dag_waves(
            [
                node("w1", ["dir:."]),
                node("w2", ["dir:src/api"]),
            ]
        )
        self.assertEqual(self.members(waves), [["w1"], ["w2"]])

    def test_trailing_slash_and_prefix_normalize(self):
        waves = plan_dag_waves(
            [
                node("w1", ["dir:src/api/"]),
                node("w2", ["src/api"]),
            ]
        )
        self.assertEqual(self.members(waves), [["w1"], ["w2"]])

    def test_sibling_directories_never_conflict(self):
        waves = plan_dag_waves(
            [
                node("w1", ["dir:src/models"]),
                node("w2", ["dir:src/model"]),
            ]
        )
        # The old token-similarity tier is gone: only subtree overlap matters.
        self.assertEqual(self.members(waves), [["w1", "w2"]])

    def test_nodes_without_owning_scopes_never_conflict(self):
        waves = plan_dag_waves([node("a"), node("b"), node("c")])
        self.assertEqual(self.members(waves), [["a", "b", "c"]])

    def test_dependency_is_min_wave_even_after_conflict(self):
        waves = plan_dag_waves(
            [
                node("w1", ["dir:src/api"]),
                node("w2", ["dir:src/api"]),
                node("w3", ["dir:src/c"], depends_on=["w2"]),
            ]
        )
        # w2 conflicts with w1 -> wave 1; w3 must be strictly after w2.
        self.assertEqual(self.members(waves), [["w1"], ["w2"], ["w3"]])

    def test_planning_is_deterministic(self):
        nodes = [
            node("w2", ["dir:src/b"], depends_on=["w1"]),
            node("w1", ["dir:src/a"]),
            node("w3", ["dir:src/a"]),
            node("w4", ["dir:src/d"]),
        ]
        first = self.members(plan_dag_waves(nodes))
        second = self.members(plan_dag_waves(nodes))
        self.assertEqual(first, second)

    def test_unknown_dependency_raises(self):
        with self.assertRaises(SlicemeError):
            plan_dag_waves([node("w1", depends_on=["ghost"])])

    def test_cycle_raises(self):
        with self.assertRaises(SlicemeError):
            plan_dag_waves(
                [
                    node("w1", depends_on=["w2"]),
                    node("w2", depends_on=["w1"]),
                ]
            )

    def test_duplicate_id_raises(self):
        with self.assertRaises(SlicemeError):
            plan_dag_waves([node("w1"), node("w1")])

    def test_wave_size_must_be_positive(self):
        with self.assertRaises(SlicemeError):
            plan_dag_waves([node("w1")], wave_size=0)

    def test_non_directory_owns_is_rejected(self):
        with self.assertRaises(SlicemeError) as ctx:
            plan_dag_waves([node("w1", ["file:src/a.py"])])
        self.assertIn("must be directories", str(ctx.exception))

    def test_validate_dag_accepts_and_rejects(self):
        validate_dag([node("w1", ["dir:src/api"]), node("w2", ["dir:docs"])])
        with self.assertRaises(SlicemeError):
            validate_dag([node("w1", ["symbol:src/a.py#A"])])


class GpuIsolationTests(unittest.TestCase):
    """A GPU node conflicts with every node, so it lands in a wave alone."""

    def test_two_gpu_nodes_land_in_two_waves(self):
        waves = plan_dag_waves(
            [
                gpu_node("g1", "T1"),
                gpu_node("g2", "T2"),
            ]
        )
        self.assertEqual([w.members for w in waves], [["g1"], ["g2"]])
        self.assertIn("g2", waves[1].conflicts)
        self.assertIn("GPU isolation", waves[1].conflicts["g2"])

    def test_gpu_node_and_cpu_node_never_share_a_wave(self):
        waves = plan_dag_waves([gpu_node("cpu", "none"), gpu_node("gpu", "T1")])
        self.assertEqual([w.members for w in waves], [["cpu"], ["gpu"]])

    def test_gpu_isolation_is_independent_of_owns(self):
        # Two GPU nodes in disjoint directories still cannot share a wave.
        waves = plan_dag_waves(
            [
                gpu_node("g1", "T1", owns=["dir:src/a"]),
                gpu_node("g2", "T1", owns=["dir:src/b"]),
            ]
        )
        self.assertEqual([w.members for w in waves], [["g1"], ["g2"]])


class ReadinessTests(unittest.TestCase):
    """``readiness`` is the spawn gate: deps done, node not yet done/running."""

    def test_nodes_without_dependencies_are_ready(self):
        nodes = [node("a"), node("b", ["dir:src/b"], depends_on=["a"])]
        self.assertEqual(readiness(nodes, {"nodes": {}}), ["a"])

    def test_dependent_is_ready_once_every_dependency_is_done(self):
        nodes = [node("a"), node("b", depends_on=["a"])]
        state = {"nodes": {"a": {"status": "done"}}}
        self.assertEqual(readiness(nodes, state), ["b"])

    def test_partial_dependencies_block_readiness(self):
        nodes = [node("a"), node("b"), node("c", depends_on=["a", "b"])]
        state = {"nodes": {"a": {"status": "done"}, "b": {"status": "pending"}}}
        self.assertEqual(readiness(nodes, state), ["b"])
        state["nodes"]["b"]["status"] = "done"
        self.assertEqual(readiness(nodes, state), ["c"])

    def test_done_and_running_nodes_are_not_ready(self):
        nodes = [node("a"), node("b")]
        state = {"nodes": {"a": {"status": "done"}, "b": {"status": "running"}}}
        self.assertEqual(readiness(nodes, state), [])

    def test_missing_state_treats_dependencies_as_pending(self):
        nodes = [node("a"), node("b", depends_on=["a"])]
        self.assertEqual(readiness(nodes, {}), ["a"])

    def test_order_follows_declaration(self):
        self.assertEqual(readiness([node("b"), node("a")], {"nodes": {}}), ["b", "a"])

    def test_readiness_does_not_need_owns(self):
        self.assertEqual(readiness([node("a")], {"nodes": {}}), ["a"])


if __name__ == "__main__":
    unittest.main()
