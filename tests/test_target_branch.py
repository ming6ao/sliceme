"""Campaign-branch selection and the delivery base.

The campaign branch is the pull request head.  It is chosen once at start: an
explicit ``feature_branch``, else ``feat/<slug(design-stem)>``.  The delivery
base (the pull request base) is the repository default branch.  Sliceme never
commits to ``main``, ``master``, or the repository default branch.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme.service import Service
from sliceme.util import SlicemeError, config_path


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class TargetBranchCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "a.txt").write_text("hi\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        self.svc = None

    def tearDown(self):
        if self.svc is not None:
            self.svc.close()
        self.tmp.cleanup()

    def test_the_design_derives_the_campaign_branch(self):
        Service.init_plane(self.root, design="DESIGN.md", checks=self.checks)
        self.svc = Service(self.root)
        self.assertEqual(self.svc.config["target_branch"], "feat/design")
        self.assertEqual(self.svc.config["main_branch"], "feat/design")
        self.assertEqual(self.svc.config["worktree_branch"], "feat/design")
        self.assertEqual(self.svc.config["delivery_base"], "main")
        self.assertEqual(self.svc.config["default_branch"], "main")

    def test_the_feature_branch_override_wins(self):
        Service.init_plane(
            self.root, feature_branch="feat/existing", checks=self.checks
        )
        self.svc = Service(self.root)
        self.assertEqual(self.svc.config["target_branch"], "feat/existing")

    def test_a_delivery_base_override_is_recorded(self):
        run("git", "branch", "develop", cwd=self.root)
        Service.init_plane(
            self.root, feature_branch="feat/x", base="develop", checks=self.checks
        )
        self.svc = Service(self.root)
        self.assertEqual(self.svc.config["delivery_base"], "develop")

    def test_a_design_or_a_feature_branch_is_required(self):
        with self.assertRaises(SlicemeError):
            Service.init_plane(self.root, checks=self.checks)

    def test_a_second_design_cannot_reuse_the_campaign_branch(self):
        # Two design names can slugify to one campaign branch.  The engine
        # refuses the collision instead of silently sharing the campaign.
        Service.init(self.root, design="web/DESIGN.md", checks=self.checks, no_unit=True)
        with self.assertRaises(SlicemeError) as ctx:
            Service.init(self.root, design="api/design.md", checks=self.checks, no_unit=True)
        self.assertIn("already belongs to design", str(ctx.exception))

    def test_start_has_no_target_parameters(self):
        # `--target` and `--target-mode` are gone: the campaign branch derives
        # from the design name, and the delivery base is the default branch.
        from sliceme import surface

        names = {param.name for param in surface.ACTION_BY_NAME["start"].params}
        self.assertNotIn("target", names)
        self.assertNotIn("target_mode", names)
        self.assertIn("design", names)
        self.assertIn("feature_branch", names)
        self.assertNotIn("target", {p.name for p in surface.ACTION_BY_NAME["deliver"].params})

    def test_the_default_branch_is_rejected_as_the_campaign_branch(self):
        with self.assertRaises(SlicemeError) as ctx:
            Service.init_plane(self.root, feature_branch="main", checks=self.checks)
        self.assertIn("campaign branch", str(ctx.exception))

    def test_the_branch_is_persisted_for_the_whole_campaign(self):
        Service.init_plane(
            self.root, feature_branch="feat/persist", checks=self.checks
        )
        import json

        first = json.loads(config_path(self.root).read_text())
        self.svc = Service(self.root)
        self.assertEqual(first["target_branch"], self.svc.config["target_branch"])
        self.assertEqual(first["worktree_branch"], self.svc.config["worktree_branch"])


if __name__ == "__main__":
    unittest.main()
