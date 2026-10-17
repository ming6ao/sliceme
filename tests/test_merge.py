"""DAG normalization: contract one directory into one node.

These pin the merge contract:
* two nodes that share one directory become one node, with or without an edge;
* the lowest topological index survives;
* the survivor waits on the union of the external dependencies;
* an absorbed id in another node's ``depends_on`` is rewritten;
* the merge is deterministic and idempotent;
* ``no_merge`` blocks the merge and raises ``revisited_directory``;
* a merge that would make a cycle raises ``revisited_directory``.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign
from sliceme.ownership import merge_same_own_nodes, plan_dag_phases, validate_dag
from sliceme.service import Service
from sliceme.util import SlicemeError, write_json


def node(nid, directory=None, depends_on=None, **extra):
    base = {
        "id": nid,
        "label": f"label {nid}",
        "goal": f"goal {nid}",
        "depends_on": depends_on or [],
        "acceptance": [f"cmd {nid}"],
    }
    if directory is not None:
        base["dir"] = directory
    base.update(extra)
    return base


def ids(nodes):
    return [n["id"] for n in nodes]


class MergeTests(unittest.TestCase):
    def test_equal_dir_on_one_edge_merges(self):
        merged, mapping = merge_same_own_nodes(
            [node("a", "src/x"), node("b", "src/x", ["a"])]
        )
        self.assertEqual(ids(merged), ["a"])
        self.assertEqual(mapping, {"b": "a"})
        self.assertEqual(merged[0]["dir"], "src/x")

    def test_equal_dir_without_an_edge_merges(self):
        merged, mapping = merge_same_own_nodes(
            [node("a", "src/x"), node("b", "src/x")]
        )
        self.assertEqual(ids(merged), ["a"])
        self.assertEqual(mapping, {"b": "a"})

    def test_different_dir_does_not_merge(self):
        merged, mapping = merge_same_own_nodes(
            [node("a", "src/x"), node("b", "src/y", ["a"])]
        )
        self.assertEqual(ids(merged), ["a", "b"])
        self.assertEqual(mapping, {})

    def test_a_different_label_still_merges(self):
        # A non-owning display field never gates a merge.
        merged, mapping = merge_same_own_nodes(
            [
                node("a", "src/x"),
                node("b", "src/x", ["a"], label="another label"),
            ]
        )
        self.assertEqual(ids(merged), ["a"])
        self.assertEqual(mapping, {"b": "a"})

    def test_three_node_chain_merges_into_one(self):
        merged, mapping = merge_same_own_nodes(
            [
                node("a", "src/x"),
                node("b", "src/x", ["a"]),
                node("c", "src/x", ["b"]),
            ]
        )
        self.assertEqual(ids(merged), ["a"])
        self.assertEqual(mapping, {"b": "a", "c": "a"})

    def test_external_dependent_is_rewired_to_the_survivor(self):
        merged, _ = merge_same_own_nodes(
            [
                node("a", "src/x"),
                node("b", "src/x", ["a"]),
                node("z", "src/z", ["b"]),
            ]
        )
        by_id = {n["id"]: n for n in merged}
        self.assertEqual(ids(merged), ["a", "z"])
        self.assertEqual(by_id["z"]["depends_on"], ["a"])

    def test_merge_is_idempotent(self):
        nodes = [
            node("a", "src/x"),
            node("b", "src/x", ["a"]),
            node("c", "src/x", ["b"]),
        ]
        merged, mapping = merge_same_own_nodes(nodes)
        again, second = merge_same_own_nodes(merged)
        self.assertEqual(mapping, {"b": "a", "c": "a"})
        self.assertEqual(second, {})
        self.assertEqual(ids(again), ids(merged))
        self.assertEqual(again, merged)

    def test_no_merge_sharing_a_directory_is_rejected(self):
        with self.assertRaises(SlicemeError) as ctx:
            merge_same_own_nodes(
                [node("a", "src/x"), node("b", "src/x", ["a"], no_merge=True)]
            )
        self.assertEqual(ctx.exception.reason, "revisited_directory")

    def test_a_revisit_that_would_make_a_cycle_is_rejected(self):
        with self.assertRaises(SlicemeError) as ctx:
            merge_same_own_nodes(
                [
                    node("a", "src/x"),
                    node("b", "src/y", ["a"]),
                    node("c", "src/x", ["b"]),
                ]
            )
        self.assertEqual(ctx.exception.reason, "revisited_directory")

    def test_fields_merge_in_dependency_order(self):
        merged, _ = merge_same_own_nodes(
            [
                node(
                    "a",
                    "src/x",
                    label="build",
                    goal="add the target",
                    acceptance=["build", "shared"],
                    tasks=["make the target"],
                ),
                node(
                    "b",
                    "src/x",
                    ["a"],
                    label="implement",
                    goal="write the source",
                    acceptance=["shared", "test"],
                    tasks=["write the source"],
                ),
            ]
        )
        self.assertEqual(len(merged), 1)
        survivor = merged[0]
        self.assertEqual(survivor["label"], "build + implement")
        self.assertIn("[a] add the target", survivor["goal"])
        self.assertIn("[b] write the source", survivor["goal"])
        self.assertEqual(survivor["acceptance"], ["build", "shared", "test"])
        self.assertEqual(survivor["tasks"], ["make the target", "write the source"])
        self.assertEqual(survivor["dir"], "src/x")
        self.assertNotIn("owns", survivor)
        self.assertEqual(survivor["depends_on"], [])
        self.assertEqual(survivor["merged_from"], ["b"])

    def test_merged_dag_keeps_validating_and_planning(self):
        nodes = [
            node("a", "src/x"),
            node("b", "src/x", ["a"]),
            node("z", "src/z", ["b"]),
        ]
        merged, _ = merge_same_own_nodes(nodes)
        validate_dag(merged)
        phases = plan_dag_phases(merged)
        self.assertEqual([w.members for w in phases], [["a"], ["z"]])

    def test_ancestor_dir_does_not_merge(self):
        merged, merge_map = merge_same_own_nodes(
            [node("a", "src"), node("b", "src/api", ["a"])]
        )
        self.assertEqual(ids(merged), ["a", "b"])
        self.assertEqual(merge_map, {})

    def test_a_multi_directory_node_is_rejected(self):
        with self.assertRaises(SlicemeError) as ctx:
            merge_same_own_nodes([{"id": "a", "owns": ["src/api", "docs"]}])
        self.assertEqual(ctx.exception.reason, "multi_directory_node")


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

    def test_normalize_reports_the_dag_path_without_a_dag(self):
        # The planning resource reads the path from `status` before any DAG
        # exists, so normalization must always name it.
        result = self.svc.normalize_dag()
        self.assertEqual(result["merged"], {})
        self.assertEqual(result["path"], str(campaign.dag_path(self.root, "feat/x")))

    def test_normalize_rewrites_the_file_and_reports_the_drop(self):
        self.write_dag([node("a", "src/x"), node("b", "src/x", ["a"])])
        result = self.svc.normalize_dag()
        self.assertEqual(result["merged"], {"b": "a"})
        self.assertEqual(result["before_nodes"], 2)
        self.assertEqual(result["after_nodes"], 1)
        self.assertEqual(result["before_phases"], 2)
        self.assertEqual(result["after_phases"], 1)
        on_disk = campaign.load_dag(self.root, "feat/x")
        self.assertEqual(ids(on_disk["nodes"]), ["a"])

    def test_status_normalizes_automatically(self):
        self.write_dag([node("a", "src/x"), node("b", "src/x", ["a"])])
        status = self.svc.status()
        self.assertEqual(status["dag_merge"]["merged"], {"b": "a"})
        self.assertEqual([w["members"] for w in status["dag_phases"]], [["a"]])
        on_disk = campaign.load_dag(self.root, "feat/x")
        self.assertEqual(ids(on_disk["nodes"]), ["a"])

    def test_normalize_rejects_removing_a_recorded_node(self):
        # A re-pass must not rewrite a recorded node, even by dropping it.
        self.write_dag([node("a", "src/x")])
        self.svc.store.create_record(
            head_commit="deadbeef",
            summary=None,
            node="b",
            campaign=self.svc.campaign_key(),
        )
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.normalize_dag()
        self.assertEqual(ctx.exception.reason, "revisited_directory")

    def test_normalize_rejects_a_progressed_duplicate(self):
        # A node with recorded progress cannot merge, so a shared directory is
        # a plan error rather than a silent rewrite.
        self.write_dag([node("a", "src/x"), node("b", "src/x", ["a"])])
        campaign.write_session(
            self.root,
            "feat/x",
            {"feature_branch": "feat/x", "nodes": {"b": {"status": "running"}}},
        )
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.normalize_dag()
        self.assertEqual(ctx.exception.reason, "revisited_directory")
        status = self.svc.status()
        self.assertIn("revisited_directory", status["dag_merge"]["error"])
        self.assertIn("revisited_directory", status["dag_phases_error"])
        on_disk = campaign.load_dag(self.root, "feat/x")
        self.assertEqual(ids(on_disk["nodes"]), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
