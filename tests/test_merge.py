"""DAG normalization: contract same-ownership chains (``sliceme/ownership.py``).

These pin the merge contract:
* two nodes that own the same directory and sit on one ``depends_on`` edge
  become one node;
* no edge or a different directory means no merge;
* the survivor waits on the union of the external dependencies;
* an absorbed id in another node's ``depends_on`` is rewritten;
* the merge is deterministic and idempotent;
* ``no_merge`` and recorded progress keep a node separate.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign
from sliceme.ownership import merge_same_own_nodes, plan_dag_waves, validate_dag
from sliceme.service import Service
from sliceme.util import write_json


def node(nid, owns, depends_on=None, **extra):
    base = {
        "id": nid,
        "label": f"label {nid}",
        "phase": "P0",
        "goal": f"goal {nid}",
        "owns": owns,
        "depends_on": depends_on or [],
        "acceptance": [f"cmd {nid}"],
        "gpu": "none",
    }
    base.update(extra)
    return base


def ids(nodes):
    return [n["id"] for n in nodes]


class MergeTests(unittest.TestCase):
    def test_equal_owns_on_one_edge_merges(self):
        merged, mapping = merge_same_own_nodes(
            [node("a", ["dir:src/x"]), node("b", ["dir:src/x"], ["a"])]
        )
        self.assertEqual(ids(merged), ["a"])
        self.assertEqual(mapping, {"b": "a"})
        self.assertEqual(merged[0]["owns"], ["dir:src/x"])

    def test_equal_owns_without_an_edge_does_not_merge(self):
        merged, mapping = merge_same_own_nodes(
            [node("a", ["dir:src/x"]), node("b", ["dir:src/x"])]
        )
        self.assertEqual(ids(merged), ["a", "b"])
        self.assertEqual(mapping, {})

    def test_different_owns_does_not_merge(self):
        merged, mapping = merge_same_own_nodes(
            [node("a", ["dir:src/x"]), node("b", ["dir:src/y"], ["a"])]
        )
        self.assertEqual(ids(merged), ["a", "b"])
        self.assertEqual(mapping, {})

    def test_a_different_phase_still_merges(self):
        # ``phase`` is a display label only; it never gates a merge.
        merged, mapping = merge_same_own_nodes(
            [
                node("a", ["dir:src/x"]),
                node("b", ["dir:src/x"], ["a"], phase="P1"),
            ]
        )
        self.assertEqual(ids(merged), ["a"])
        self.assertEqual(mapping, {"b": "a"})

    def test_three_node_chain_merges_into_one(self):
        merged, mapping = merge_same_own_nodes(
            [
                node("a", ["dir:src/x"]),
                node("b", ["dir:src/x"], ["a"]),
                node("c", ["dir:src/x"], ["b"]),
            ]
        )
        self.assertEqual(ids(merged), ["a"])
        self.assertEqual(mapping, {"b": "a", "c": "a"})

    def test_external_dependent_is_rewired_to_the_survivor(self):
        merged, _ = merge_same_own_nodes(
            [
                node("a", ["dir:src/x"]),
                node("b", ["dir:src/x"], ["a"]),
                node("z", ["dir:src/z"], ["b"]),
            ]
        )
        by_id = {n["id"]: n for n in merged}
        self.assertEqual(ids(merged), ["a", "z"])
        self.assertEqual(by_id["z"]["depends_on"], ["a"])

    def test_merge_is_idempotent(self):
        nodes = [
            node("a", ["dir:src/x"]),
            node("b", ["dir:src/x"], ["a"]),
            node("c", ["dir:src/x"], ["b"]),
        ]
        merged, mapping = merge_same_own_nodes(nodes)
        again, second = merge_same_own_nodes(merged)
        self.assertEqual(mapping, {"b": "a", "c": "a"})
        self.assertEqual(second, {})
        self.assertEqual(ids(again), ids(merged))
        self.assertEqual(again, merged)

    def test_no_merge_keeps_a_node_separate(self):
        merged, mapping = merge_same_own_nodes(
            [
                node("a", ["dir:src/x"]),
                node("b", ["dir:src/x"], ["a"], no_merge=True),
            ]
        )
        self.assertEqual(ids(merged), ["a", "b"])
        self.assertEqual(mapping, {})

    def test_fields_merge_in_dependency_order(self):
        merged, _ = merge_same_own_nodes(
            [
                node(
                    "a",
                    ["dir:src/x"],
                    label="build",
                    goal="add the target",
                    acceptance=["build", "shared"],
                    gpu="none",
                ),
                node(
                    "b",
                    ["dir:src/x"],
                    ["a"],
                    label="implement",
                    goal="write the source",
                    acceptance=["shared", "test"],
                    gpu="T2",
                ),
            ]
        )
        self.assertEqual(len(merged), 1)
        survivor = merged[0]
        self.assertEqual(survivor["label"], "build + implement")
        self.assertIn("[a] add the target", survivor["goal"])
        self.assertIn("[b] write the source", survivor["goal"])
        self.assertEqual(survivor["acceptance"], ["build", "shared", "test"])
        self.assertEqual(survivor["gpu"], "T2")
        self.assertEqual(survivor["owns"], ["dir:src/x"])
        self.assertEqual(survivor["depends_on"], [])
        self.assertEqual(survivor["merged_from"], ["b"])

    def test_merged_dag_keeps_validating_and_planning(self):
        nodes = [
            node("a", ["dir:src/x"]),
            node("b", ["dir:src/x"], ["a"]),
            node("z", ["dir:src/z"], ["b"]),
        ]
        merged, _ = merge_same_own_nodes(nodes)
        validate_dag(merged)
        waves = plan_dag_waves(merged)
        self.assertEqual([w.members for w in waves], [["a"], ["z"]])

    def test_ancestor_owns_does_not_merge(self):
        merged, merge_map = merge_same_own_nodes(
            [node("a", ["dir:src"]), node("b", ["dir:src/api"], ["a"])]
        )
        self.assertEqual(ids(merged), ["a", "b"])
        self.assertEqual(merge_map, {})


def cpu_backend_dag():
    """The chain from the ``cpu-backend`` campaign (13 nodes)."""
    return [
        node("p0-bench-build", ["dir:dev/kernels"], phase="P0"),
        node("p0-bench-micro", ["dir:dev/kernels"], ["p0-bench-build"], phase="P0"),
        node("p0-baseline", ["dir:docs"], ["p0-bench-micro"], phase="P0"),
        node("p1-build-aggregate", ["dir:backends/cpu"], ["p0-baseline"], phase="P1"),
        node("p1-gemm-reorder", ["dir:backends/cpu"], ["p1-build-aggregate"], phase="P1"),
        node("p1-gate", ["dir:dev/kernels", "dir:docs"], ["p1-gemm-reorder"], phase="P1"),
        node("p2-blocking", ["dir:backends/cpu"], ["p1-gate"], phase="P2"),
        node("p2-record", ["dir:docs"], ["p2-blocking"], phase="P2"),
        node("p3-muon", ["dir:backends/cpu"], ["p2-record"], phase="P3"),
        node("p3-classifier", ["dir:backends/cpu"], ["p3-muon"], phase="P3"),
        node("p3-attention", ["dir:backends/cpu"], ["p3-classifier"], phase="P3"),
        node("p3-e2e-gate", ["dir:dev/kernels", "dir:docs"], ["p3-attention"], phase="P3"),
        node("p4-promote", ["dir:docs"], ["p3-e2e-gate"], phase="P4"),
    ]


class CpuBackendCase(unittest.TestCase):
    def test_campaign_chain_loses_four_nodes_and_four_waves(self):
        original = cpu_backend_dag()
        before = plan_dag_waves(original)
        merged, mapping = merge_same_own_nodes(original)
        after = plan_dag_waves(merged)
        self.assertEqual(len(before), 13)
        self.assertEqual(len(original), 13)
        self.assertEqual(len(merged), 9)
        self.assertEqual(len(after), 9)
        self.assertEqual(
            mapping,
            {
                "p0-bench-micro": "p0-bench-build",
                "p1-gemm-reorder": "p1-build-aggregate",
                "p3-classifier": "p3-muon",
                "p3-attention": "p3-muon",
            },
        )
        # The baseline now waits on the survivor of the Phase 0 merge.
        by_id = {n["id"]: n for n in merged}
        self.assertEqual(by_id["p0-baseline"]["depends_on"], ["p0-bench-build"])


class NormalizeDagTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=self.root, check=True)
        (self.root / "a.txt").write_text("a\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root, check=True)
        Service.init_plane(
            self.root,
            feature_branch="feat/x",
            checks=[{"name": "ok", "command": "true", "required": True}],
        )
        self.svc = Service(self.root)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def write_dag(self, nodes):
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "demo",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": 3,
                "nodes": nodes,
            },
        )

    def test_normalize_rewrites_the_file_and_reports_the_drop(self):
        self.write_dag([node("a", ["dir:src/x"]), node("b", ["dir:src/x"], ["a"])])
        result = self.svc.normalize_dag()
        self.assertEqual(result["merged"], {"b": "a"})
        self.assertEqual(result["before_nodes"], 2)
        self.assertEqual(result["after_nodes"], 1)
        self.assertEqual(result["before_waves"], 2)
        self.assertEqual(result["after_waves"], 1)
        on_disk = campaign.load_dag(self.root, "feat/x")
        self.assertEqual(ids(on_disk["nodes"]), ["a"])

    def test_status_normalizes_automatically(self):
        self.write_dag([node("a", ["dir:src/x"]), node("b", ["dir:src/x"], ["a"])])
        status = self.svc.status()
        self.assertEqual(status["dag_merge"]["merged"], {"b": "a"})
        self.assertEqual([w["members"] for w in status["dag_waves"]], [["a"]])
        on_disk = campaign.load_dag(self.root, "feat/x")
        self.assertEqual(ids(on_disk["nodes"]), ["a"])

    def test_normalize_protects_a_node_with_progress(self):
        self.write_dag([node("a", ["dir:src/x"]), node("b", ["dir:src/x"], ["a"])])
        write_json(
            campaign.state_path(self.root, "feat/x"),
            {"nodes": {"b": {"status": "done"}}},
        )
        result = self.svc.normalize_dag()
        self.assertEqual(result["merged"], {})
        on_disk = campaign.load_dag(self.root, "feat/x")
        self.assertEqual(ids(on_disk["nodes"]), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
