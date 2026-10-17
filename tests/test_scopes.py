"""Unit tests for directory ownership (``sliceme/ownership.py``)."""

import unittest

from sliceme.ownership import (
    deepest_owner_of,
    node_dir,
    normalize_dir,
    owns_conflict,
    parse_dir,
    parse_owns,
    path_within_owns,
)
from sliceme.util import SlicemeError


class NormalizeTests(unittest.TestCase):
    def test_trailing_slash(self):
        self.assertEqual(normalize_dir("src/api"), "src/api")
        self.assertEqual(normalize_dir("src/api/"), "src/api")
        self.assertEqual(normalize_dir("./src/api/"), "src/api")

    def test_root_normalizes_to_dot(self):
        self.assertEqual(normalize_dir("."), ".")
        self.assertEqual(normalize_dir(""), ".")
        self.assertEqual(normalize_dir("/"), ".")


class ParseOwnsTests(unittest.TestCase):
    def test_accepts_bare_paths(self):
        self.assertEqual(parse_owns(["src/api", "docs"]), ["src/api", "docs"])

    def test_dedupes_normalized_paths(self):
        self.assertEqual(parse_owns(["src/api/", "src/api"]), ["src/api"])

    def test_rejects_kind_prefixes(self):
        for spec in ("file:src/a.py", "symbol:src/a.py#A", "api:GET /x", "dir:src/api"):
            with self.assertRaises(SlicemeError):
                parse_owns([spec])

    def test_rejects_empty_or_whitespace_entry(self):
        for spec in ("", "   ", "\t"):
            with self.assertRaises(SlicemeError):
                parse_owns([spec])
        with self.assertRaises(SlicemeError):
            parse_owns(["src/api", ""])

    def test_missing_owns_list_stays_valid(self):
        self.assertEqual(parse_owns([]), [])


class ConflictTests(unittest.TestCase):
    def test_equal_directories_conflict(self):
        self.assertIsNotNone(owns_conflict(["src/api"], ["src/api"]))

    def test_ancestor_conflicts(self):
        self.assertIsNotNone(owns_conflict(["src"], ["src/api"]))
        self.assertIsNotNone(owns_conflict(["src/api"], ["src"]))

    def test_siblings_do_not_conflict(self):
        self.assertIsNone(owns_conflict(["src/models"], ["src/model"]))
        self.assertIsNone(owns_conflict(["src/a"], ["src/b"]))

    def test_empty_owns_never_conflicts(self):
        self.assertIsNone(owns_conflict([], ["src/api"]))
        self.assertIsNone(owns_conflict(["src/api"], []))

    def test_root_conflicts_with_everything(self):
        self.assertIsNotNone(owns_conflict(["."], ["src/api"]))


class PathWithinOwnsTests(unittest.TestCase):
    def test_file_inside_owned_dir(self):
        self.assertTrue(path_within_owns("src/api/routes.py", ["src/api"]))
        self.assertTrue(path_within_owns("src/api/v1/x.py", ["src/api"]))

    def test_file_outside_owned_dir(self):
        self.assertFalse(path_within_owns("docs/api.md", ["src/api"]))
        self.assertFalse(path_within_owns("src/other.py", ["src/api"]))

    def test_root_owns_everything(self):
        self.assertTrue(path_within_owns("docs/api.md", ["."]))
        self.assertTrue(path_within_owns("a.txt", ["."]))


class ParseDirTests(unittest.TestCase):
    def test_normalizes_one_path(self):
        self.assertEqual(parse_dir("src/api/"), "src/api")
        self.assertEqual(parse_dir("src/api"), "src/api")
        self.assertEqual(parse_dir("."), ".")

    def test_rejects_empty_and_kind_prefix(self):
        for spec in ("", "   ", "file:src/a.py", "dir:src/api"):
            with self.assertRaises(SlicemeError):
                parse_dir(spec)


class NodeDirTests(unittest.TestCase):
    def test_reads_dir(self):
        self.assertEqual(node_dir({"id": "a", "dir": "src/api"}), "src/api")

    def test_reads_the_one_entry_owns_shim(self):
        self.assertEqual(node_dir({"id": "a", "owns": ["src/api/"]}), "src/api")

    def test_dir_and_a_matching_shim_are_accepted(self):
        self.assertEqual(
            node_dir({"id": "a", "dir": "src/api", "owns": ["src/api"]}),
            "src/api",
        )

    def test_rejects_two_owns_entries(self):
        with self.assertRaises(SlicemeError) as ctx:
            node_dir({"id": "a", "owns": ["src/api", "docs"]})
        self.assertEqual(ctx.exception.reason, "multi_directory_node")

    def test_rejects_a_conflicting_shim(self):
        with self.assertRaises(SlicemeError) as ctx:
            node_dir({"id": "a", "dir": "src/api", "owns": ["docs"]})
        self.assertEqual(ctx.exception.reason, "conflicting_directory")

    def test_no_directory_returns_none(self):
        self.assertIsNone(node_dir({"id": "a"}))
        self.assertIsNone(node_dir({"id": "a", "owns": []}))


class DeepestOwnerTests(unittest.TestCase):
    def test_the_deepest_directory_wins(self):
        dirs = {"top": "src", "api": "src/api", "docs": "docs"}
        self.assertEqual(deepest_owner_of("src/api/x.py", dirs), "api")
        self.assertEqual(deepest_owner_of("src/top.py", dirs), "top")
        self.assertEqual(deepest_owner_of("docs/guide.md", dirs), "docs")

    def test_the_root_is_the_shallowest_match(self):
        dirs = {"root": ".", "api": "src/api"}
        self.assertEqual(deepest_owner_of("src/api/x.py", dirs), "api")
        self.assertEqual(deepest_owner_of("README.md", dirs), "root")

    def test_an_unowned_path_has_no_owner(self):
        self.assertIsNone(deepest_owner_of("other/x.py", {"a": "src/api"}))

    def test_a_missing_directory_never_matches(self):
        self.assertIsNone(deepest_owner_of("src/api/x.py", {"a": None}))


if __name__ == "__main__":
    unittest.main()
