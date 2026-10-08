"""Multi-campaign isolation in one plane.

These pin the contract of ``docs/multi-campaign.md``:

* one plane holds several campaigns, each with its own target branch, worktree,
  DAG, and review queue;
* candidates, checks, and review rows stay scoped to one campaign;
* a delivery marks only its own campaign landed;
* the campaign registry migrates an old one-campaign plane.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign
from sliceme.service import Service
from sliceme.store import Store
from sliceme.util import write_json

FAKE_GH_BIN = Path(__file__).resolve().parent / "bin"


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


DAG = {
    "campaign": "demo",
    "base": "main",
    "concurrency": 2,
    "nodes": [
        {"id": "w1", "owns": ["dir:src/a"], "depends_on": [], "acceptance": ["true"]},
        {"id": "w2", "owns": ["dir:src/b"], "depends_on": [], "acceptance": ["true"]},
    ],
}


class MultiCampaignCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote_tmp = tempfile.TemporaryDirectory()
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "src" / "a").mkdir(parents=True)
        (self.root / "src" / "b").mkdir(parents=True)
        (self.root / "src" / "a" / "x.py").write_text("a = 1\n")
        (self.root / "src" / "b" / "y.py").write_text("b = 1\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        remote = Path(self.remote_tmp.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        run("git", "remote", "add", "origin", str(remote), cwd=self.root)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(FAKE_GH_BIN) + os.pathsep + self._old_path
        Service.init_plane(
            self.root,
            feature_branch="feat/x",
            base="main",
            checks=self.checks,
        )
        self.svc = Service(self.root)
        self.svc.close()
        # A second campaign with a different campaign branch.
        Service.init(
            self.root,
            feature_branch="feat/y",
            base="main",
            no_unit=True,
        )
        # The campaign branch is the pull request head and the worktree branch.
        # The fixture creates it from the delivery base; a real run opens the
        # worktree from the remote.
        run("git", "branch", "feat/x", "main", cwd=self.root)
        run("git", "branch", "feat/y", "main", cwd=self.root)

    def tearDown(self):
        os.environ["PATH"] = self._old_path
        self.remote_tmp.cleanup()
        self.tmp.cleanup()

    def service(self, ref=None):
        return Service(self.root, campaign=ref)

    def write_dag(self, branch):
        write_json(
            campaign.dag_path(self.root, branch),
            {**DAG, "feature_branch": branch},
        )

    def edit(self, worktree, rel, content):
        (Path(worktree) / rel).write_text(content)

    def record(self, ref, branch, content_a="a = 2\n"):
        svc = self.service(ref)
        try:
            self.write_dag(branch)
            unit = svc.create_campaign_workspace(base="main")
            self.edit(unit["worktree"], "src/a/x.py", content_a)
            return svc.record_wave(0, messages={"w1": "change a"})
        finally:
            svc.close()

    def file_on(self, branch, rel):
        return run("git", "show", f"{branch}:{rel}", cwd=self.root).stdout


class RegistryTests(MultiCampaignCase):
    def test_two_campaigns_are_registered(self):
        svc = self.service()
        try:
            rows = svc.store.list_campaigns()
            self.assertEqual([r["target_branch"] for r in rows], ["feat/x", "feat/y"])
            self.assertEqual([r["key"] for r in rows], ["feat--x", "feat--y"])
            self.assertEqual(len({r["unit_name"] for r in rows}), 2)
            self.assertEqual(len({r["worktree_branch"] for r in rows}), 2)
        finally:
            svc.close()

    def test_each_campaign_has_its_own_worktree_and_dag(self):
        self.record("feat/x", "feat/x")
        self.record("feat/y", "feat/y", content_a="a = 3\n")
        x = self.service("feat/x")
        y = self.service("feat/y")
        try:
            xu = x.create_campaign_workspace()
            yu = y.create_campaign_workspace()
            self.assertNotEqual(xu["worktree"], yu["worktree"])
            self.assertNotEqual(xu["branch"], yu["branch"])
            self.assertNotEqual(
                campaign.dag_path(self.root, "feat/x"),
                campaign.dag_path(self.root, "feat/y"),
            )
        finally:
            x.close()
            y.close()

    def test_ambiguous_plane_requires_a_campaign(self):
        svc = self.service()
        try:
            with self.assertRaises(Exception) as ctx:
                _ = svc.config
            self.assertIn("several campaigns", str(ctx.exception))
        finally:
            svc.close()


class ScopeTests(MultiCampaignCase):
    def test_candidates_stay_in_their_campaign(self):
        self.record("feat/x", "feat/x")
        self.record("feat/y", "feat/y", content_a="a = 3\n")
        svc = self.service()
        try:
            x = svc.store.list_candidates(campaign="feat--x")
            y = svc.store.list_candidates(campaign="feat--y")
            self.assertTrue(x)
            self.assertTrue(y)
            self.assertNotEqual([c["id"] for c in x], [c["id"] for c in y])
            self.assertTrue(all(c["campaign"] == "feat--x" for c in x))
            self.assertTrue(all(c["campaign"] == "feat--y" for c in y))
        finally:
            svc.close()

    def test_status_scopes_units_and_candidates(self):
        self.record("feat/x", "feat/x")
        self.record("feat/y", "feat/y", content_a="a = 3\n")
        x = self.service("feat/x")
        y = self.service("feat/y")
        try:
            xs = x.status()
            ys = y.status()
            self.assertEqual(xs["target_branch"], "feat/x")
            self.assertEqual(ys["target_branch"], "feat/y")
            self.assertTrue(all(c["campaign"] == "feat--x" for c in xs["candidates"]))
            self.assertTrue(all(c["campaign"] == "feat--y" for c in ys["candidates"]))
        finally:
            x.close()
            y.close()

    def test_plane_status_lists_every_campaign(self):
        self.record("feat/x", "feat/x")
        self.record("feat/y", "feat/y", content_a="a = 3\n")
        svc = self.service()
        try:
            plane = svc.status()
            self.assertTrue(plane.get("plane"))
            keys = {c["key"] for c in plane["campaigns"]}
            self.assertEqual(keys, {"feat--x", "feat--y"})
        finally:
            svc.close()


class DeliveryIsolationTests(MultiCampaignCase):
    def test_delivering_one_campaign_leaves_the_other(self):
        self.record("feat/x", "feat/x")
        self.record("feat/y", "feat/y", content_a="a = 3\n")
        x = self.service("feat/x")
        try:
            x.review_decision(action="approve", all_commits=True, actor="test")
            result = x.deliver()
            self.assertEqual([r["status"] for r in result["results"]], ["landed"])
            self.assertTrue(result["pull_request"]["url"])
            worktree_branch = x.store.get_campaign("feat--x")["worktree_branch"]
        finally:
            x.close()
        # The approved work is on the campaign branch; the delivery base is
        # untouched.
        self.assertEqual(self.file_on(worktree_branch, "src/a/x.py"), "a = 2\n")
        self.assertEqual(self.file_on("main", "src/a/x.py"), "a = 1\n")
        # The other campaign keeps its own unlanded work.
        self.assertEqual(self.file_on("feat/y", "src/a/x.py"), "a = 3\n")
        y = self.service("feat/y")
        try:
            self.assertTrue(
                all(c["status"] == "prepared" for c in y.store.list_candidates(campaign="feat--y"))
            )
            self.assertEqual(y.store.get_campaign("feat--y")["state"], "working")
            self.assertEqual(y.store.get_campaign("feat--x")["state"], "delivered")
        finally:
            y.close()

    def test_review_packet_is_scoped(self):
        self.record("feat/x", "feat/x")
        self.record("feat/y", "feat/y", content_a="a = 3\n")
        x = self.service("feat/x")
        y = self.service("feat/y")
        try:
            xp = x.review_snapshot()
            yp = y.review_snapshot()
            self.assertEqual(xp["branch_key"], "feat--x")
            self.assertEqual(yp["branch_key"], "feat--y")
            self.assertTrue(xp["commits"])
            self.assertTrue(yp["commits"])
            self.assertNotEqual(
                {c["hash"] for c in xp["commits"]},
                {c["hash"] for c in yp["commits"]},
            )
        finally:
            x.close()
            y.close()


class CleanupTests(MultiCampaignCase):
    def write_campaign_files(self, branch):
        """Write one of every per-campaign file and return the disposable ones."""
        write_json(campaign.dag_path(self.root, branch), {"nodes": []})
        write_json(campaign.state_path(self.root, branch), {"nodes": {}})
        write_json(campaign.session_path(self.root, branch), {"status": "suspended"})
        write_json(campaign.control_path(self.root, branch), {"pause": True})
        campaign.worker_log_path(self.root, branch, "w1").write_text("log\n")
        campaign.report_path(self.root, branch).write_text("# report\n")
        return [
            campaign.dag_path(self.root, branch),
            campaign.state_path(self.root, branch),
            campaign.session_path(self.root, branch),
            campaign.control_path(self.root, branch),
            campaign.worker_log_path(self.root, branch, "w1"),
        ]

    def test_remove_campaign_artifacts_removes_every_file_but_the_report(self):
        svc = self.service("feat/x")
        try:
            files = self.write_campaign_files("feat/x")
            removed = svc.remove_campaign_artifacts()
            self.assertEqual(sorted(removed), sorted(str(path) for path in files))
            for path in files:
                self.assertFalse(path.exists(), str(path))
            self.assertTrue(campaign.report_path(self.root, "feat/x").exists())
        finally:
            svc.close()

    def test_remove_campaign_artifacts_can_drop_the_report(self):
        svc = self.service("feat/x")
        try:
            self.write_campaign_files("feat/x")
            svc.remove_campaign_artifacts(keep_report=False)
            self.assertFalse(campaign.report_path(self.root, "feat/x").exists())
        finally:
            svc.close()

    def test_gc_artifacts_prunes_only_finished_campaigns(self):
        x_files = self.write_campaign_files("feat/x")
        y_files = self.write_campaign_files("feat/y")
        svc = self.service()
        try:
            svc.store.set_campaign_state("feat--x", "delivered")
            svc.store.conn.commit()

            result = svc.gc(artifacts=True)

            self.assertIn(
                str(campaign.dag_path(self.root, "feat/x")), result["pruned_artifacts"]
            )
            for path in x_files:
                self.assertFalse(path.exists(), str(path))
            for path in y_files:
                self.assertTrue(path.exists(), str(path))
            # The report of a finished campaign stays as evidence.
            self.assertTrue(campaign.report_path(self.root, "feat/x").exists())
        finally:
            svc.close()

    def test_gc_without_artifacts_keeps_finished_campaign_files(self):
        files = self.write_campaign_files("feat/x")
        svc = self.service()
        try:
            svc.store.set_campaign_state("feat--x", "delivered")
            svc.store.conn.commit()
            svc.gc()
            for path in files:
                self.assertTrue(path.exists(), str(path))
        finally:
            svc.close()

    def test_gc_artifacts_prunes_a_completed_descriptor(self):
        files = self.write_campaign_files("feat/z")
        campaign.write_session(
            self.root, "feat/z", {"status": "completed", "feature_branch": "feat/z"}
        )
        svc = self.service()
        try:
            result = svc.gc(artifacts=True)
            self.assertIn(
                str(campaign.session_path(self.root, "feat/z")),
                result["pruned_artifacts"],
            )
            for path in files:
                self.assertFalse(path.exists(), str(path))
            self.assertFalse(campaign.session_path(self.root, "feat/z").exists())
            self.assertTrue(campaign.report_path(self.root, "feat/z").exists())
        finally:
            svc.close()

    def test_gc_artifacts_keeps_a_suspended_descriptor(self):
        files = self.write_campaign_files("feat/z")
        campaign.write_session(
            self.root, "feat/z", {"status": "suspended", "feature_branch": "feat/z"}
        )
        svc = self.service()
        try:
            svc.gc(artifacts=True)
            for path in files:
                self.assertTrue(path.exists(), str(path))
        finally:
            svc.close()

    def test_prune_keeps_every_registered_campaign(self):
        svc = self.service()
        try:
            for key in ("feat--x", "feat--y", "feat--gone"):
                svc.store.add_review_decision(branch_key=key, action="approve")
            svc.store.conn.commit()
            # Force every row past the retention window.
            svc.store.conn.execute("UPDATE review_decisions SET created_at=0")
            svc.store.conn.commit()
            svc._prune_reviews()
            kept = {d["branch_key"] for d in svc.store.list_review_decisions()}
            self.assertIn("feat--x", kept)
            self.assertIn("feat--y", kept)
            self.assertNotIn("feat--gone", kept)
        finally:
            svc.close()


class MigrationTests(MultiCampaignCase):
    def test_an_old_plane_becomes_one_campaign(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run("git", "init", "-q", "-b", "main", cwd=root)
            run("git", "config", "user.email", "t@example.com", cwd=root)
            run("git", "config", "user.name", "Tester", cwd=root)
            (root / "a.txt").write_text("hi\n")
            run("git", "add", "-A", cwd=root)
            run("git", "commit", "-qm", "initial", cwd=root)
            Service.init_plane(root, feature_branch="feat/old", checks=self.checks)
            # Simulate a plane from before campaign rows existed.
            store = Store(root)
            store.conn.execute("DELETE FROM campaigns")
            store.conn.commit()
            store.close()
            svc = Service(root)
            try:
                rows = svc.store.list_campaigns()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["target_branch"], "feat/old")
                self.assertEqual(rows[0]["unit_name"], "campaign")
                self.assertEqual(svc.config["target_branch"], "feat/old")
            finally:
                svc.close()

    def test_a_migrated_plane_keeps_one_branch_across_consumers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run("git", "init", "-q", "-b", "main", cwd=root)
            run("git", "config", "user.email", "t@example.com", cwd=root)
            run("git", "config", "user.name", "Tester", cwd=root)
            (root / "a.txt").write_text("hi\n")
            run("git", "add", "-A", cwd=root)
            run("git", "commit", "-qm", "initial", cwd=root)
            # A plane from the older two-branch model: the target feature branch
            # and a separate accumulation branch for the campaign worktree.
            run("git", "branch", "feat/old", "main", cwd=root)
            run("git", "branch", "sliceme/feat-old", "main", cwd=root)
            from sliceme.util import config_path

            write_json(
                config_path(root),
                {
                    "version": 1,
                    "target_branch": "feat/old",
                    "main_branch": "feat/old",
                    "worktree_branch": "sliceme/feat-old",
                    "base": "main",
                    "checks": self.checks,
                    "policy": {"remote": "origin"},
                    "created_at": 0,
                },
            )
            svc = Service(root)
            try:
                row = svc.store.get_campaign("feat/old")
                self.assertEqual(row["worktree_branch"], "sliceme/feat-old")
                unit = svc.create_campaign_workspace()
                # The worktree branch is the one every consumer records, so the
                # worktree, the review source, and the pull request head agree.
                self.assertEqual(unit["branch"], "sliceme/feat-old")
                self.assertEqual(svc.config["worktree_branch"], "sliceme/feat-old")
            finally:
                svc.close()

    def test_a_null_delivery_base_is_backfilled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run("git", "init", "-q", "-b", "main", cwd=root)
            run("git", "config", "user.email", "t@example.com", cwd=root)
            run("git", "config", "user.name", "Tester", cwd=root)
            (root / "a.txt").write_text("hi\n")
            run("git", "add", "-A", cwd=root)
            run("git", "commit", "-qm", "initial", cwd=root)
            Service.init_plane(root, feature_branch="feat/old", checks=self.checks)
            store = Store(root)
            # A row from before the delivery base column existed.
            store.conn.execute("UPDATE campaigns SET delivery_base=NULL")
            store.conn.commit()
            store.close()
            store = Store(root)
            try:
                self.assertEqual(
                    store.get_campaign("feat/old")["delivery_base"], "main"
                )
            finally:
                store.close()

    def test_campaign_registration_is_idempotent(self):
        svc = self.service()
        try:
            again = svc.store.create_campaign(
                key="feat--x",
                target_branch="feat/x",
                worktree_branch="feat/x",
                unit_name="campaign:feat--x",
            )
            self.assertEqual(again["target_branch"], "feat/x")
            self.assertEqual(len(svc.store.list_campaigns()), 2)
        finally:
            svc.close()

if __name__ == "__main__":
    unittest.main()
