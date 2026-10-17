"""One workspace lock serializes campaign creation, record, and delivery.

``docs/terminology-and-simplification-plan.md`` S6: the campaign-creation,
phase-record, and delivery paths all take the single ``workspace.lock``; there is
no separate delivery lock.
"""

import fcntl
import tempfile
import unittest
from pathlib import Path

from sliceme.service import workspace_lock
from sliceme.util import state_dir


class WorkspaceLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def lock_path(self) -> Path:
        return state_dir(self.root) / "workspace.lock"

    def test_the_lock_is_held_exclusively_while_work_runs(self):
        with workspace_lock(self.root):
            self.assertTrue(self.lock_path().exists())
            with self.lock_path().open("a", encoding="utf-8") as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Released: the same non-blocking acquire now succeeds.
        with self.lock_path().open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def test_record_and_delivery_share_the_one_lock_file(self):
        with workspace_lock(self.root):
            self.assertFalse((state_dir(self.root) / "review.lock").exists())
            self.assertFalse((state_dir(self.root) / "campaigns.lock").exists())


if __name__ == "__main__":
    unittest.main()
