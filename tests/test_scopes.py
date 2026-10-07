"""Unit tests for directory ownership (``sliceme/ownership.py``)."""

import unittest

from sliceme.ownership import (
    normalize_dir,
    owns_conflict,
    parse_owns,
    path_within_owns,
)
from sliceme.util import SlicemeError


class NormalizeTests(unittest.TestCase):
    def test_dir_prefix_and_trailing_slash(self):
        self.assertEqual(normalize_dir("dir:src/api"), "src/api")
        self.assertEqual(normalize_dir("src/api/"), "src/api")
        self.assertEqual(normalize_dir("./src/api/"), "src/api")

    def test_root_normalizes_to_dot(self):
        self.assertEqual(normalize_dir("dir:."), ".")
        self.assertEqual(normalize_dir(""), ".")
        self.assertEqual(normalize_dir("/"), ".")


class ParseOwnsTests(unittest.TestCase):
    def test_accepts_dir_and_bare_paths(self):
        self.assertEqual(parse_owns(["dir:src/api", "docs"]), ["src/api", "docs"])

    def test_dedupes_normalized_paths(self):
        self.assertEqual(parse_owns(["dir:src/api/", "src/api"]), ["src/api"])

    def test_rejects_non_directory_kinds(self):
        for spec in ("file:src/a.py", "symbol:src/a.py#A", "api:GET /x"):
            with self.assertRaises(SlicemeError):
                parse_owns([spec])

    def test_rejects_empty_or_whitespace_entry(self):
        for spec in ("", "   ", "\t"):
            with self.assertRaises(SlicemeError):
                parse_owns([spec])
        with self.assertRaises(SlicemeError):
            parse_owns(["dir:src/api", ""])

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


if __name__ == "__main__":
    unittest.main()
