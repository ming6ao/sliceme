"""Campaign budget: the caps, the pause reason, and the `budget` action.

These pin the engine side of the automatic budget suspend
(``docs/sessions.md`` §10):

* ``start`` stores the wall-clock and cost caps in ``config.json``;
* ``ready`` and ``status`` report the caps, ``paused``, and the pause reason;
* a budget pause reason is distinct from a user suspension;
* the ``budget`` action raises or clears the caps and opens a new window;
* an older config without ``budget`` stays valid (no cap).
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign, surface
from sliceme.cli import _normalize_argv, build_parser
from sliceme.service import Service
from sliceme.util import SlicemeError, config_path, read_json, write_json


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class BudgetCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "src" / "a").mkdir(parents=True)
        (self.root / "src" / "a" / "x.py").write_text("a = 1\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        self.svc = None

    def tearDown(self):
        if self.svc is not None:
            self.svc.close()
        self.tmp.cleanup()

    def init(self, **kwargs):
        Service.init_plane(self.root, feature_branch="feat/x", checks=self.checks, **kwargs)
        self.svc = Service(self.root)
        return self.svc

    def config(self):
        return read_json(config_path(self.root))


class StartBudgetTests(BudgetCase):
    def test_start_stores_both_caps(self):
        svc = self.init(budget_seconds=3600, budget_usd=12.5)
        cfg = self.config()
        self.assertEqual(cfg["budget"], {"wall_seconds": 3600, "cost_usd": 12.5})
        self.assertGreater(cfg["budget_started_at"], 0)
        self.assertEqual(svc.ready()["budget"], {"wall_seconds": 3600, "cost_usd": 12.5})

    def test_start_without_caps_omits_the_budget(self):
        svc = self.init()
        self.assertNotIn("budget", self.config())
        self.assertIsNone(svc.ready()["budget"])
        self.assertIsNone(svc.status_summary()["budget"])

    def test_a_non_positive_cap_is_rejected(self):
        for kwargs in ({"budget_seconds": 0}, {"budget_seconds": -5}, {"budget_usd": 0}):
            with self.assertRaises(SlicemeError):
                Service.init_plane(
                    self.root, feature_branch="feat/x", checks=self.checks, **kwargs
                )


class PauseReasonTests(BudgetCase):
    def test_ready_reports_the_budget_reason(self):
        svc = self.init(budget_seconds=60)
        campaign.write_control(
            self.root,
            "feat/x",
            {"pause": True, "reason": "budget", "metric": "wall_seconds", "requested_at": 1.0},
        )
        ready = svc.ready()
        self.assertTrue(ready["paused"])
        self.assertEqual(ready["pause_reason"], "budget")
        self.assertEqual(ready["pause_metric"], "wall_seconds")
        status = svc.status()
        self.assertTrue(status["paused"])
        self.assertEqual(status["pause_reason"], "budget")
        self.assertEqual(status["budget"], {"wall_seconds": 60})

    def test_a_user_pause_has_no_metric(self):
        svc = self.init()
        campaign.write_control(
            self.root, "feat/x", {"pause": True, "reason": "user", "requested_at": 1.0}
        )
        self.assertTrue(svc.ready()["paused"])
        self.assertEqual(svc.ready()["pause_reason"], "user")
        self.assertIsNone(svc.ready()["pause_metric"])

    def test_no_control_file_reports_no_reason(self):
        svc = self.init(budget_usd=1.0)
        self.assertFalse(svc.ready()["paused"])
        self.assertIsNone(svc.ready()["pause_reason"])


class BudgetActionTests(BudgetCase):
    def test_raise_keeps_the_other_cap_and_opens_a_window(self):
        svc = self.init(budget_seconds=60, budget_usd=5.0)
        first_window = self.config()["budget_started_at"]
        result = svc.set_budget(seconds=120)
        self.assertEqual(result["budget"], {"wall_seconds": 120, "cost_usd": 5.0})
        self.assertGreaterEqual(self.config()["budget_started_at"], first_window)

    def test_clear_removes_both_caps(self):
        svc = self.init(budget_seconds=60, budget_usd=5.0)
        result = svc.set_budget(clear=True)
        self.assertIsNone(result["budget"])
        self.assertNotIn("budget", self.config())

    def test_an_empty_call_is_rejected(self):
        svc = self.init(budget_seconds=60)
        with self.assertRaises(SlicemeError):
            svc.set_budget()

    def test_dispatch_sets_the_caps(self):
        svc = self.init()
        result = surface.dispatch(svc, "budget", {"seconds": 30, "usd": 2.0})
        self.assertEqual(result["budget"], {"wall_seconds": 30, "cost_usd": 2.0})

    def test_clear_keeps_a_window_start(self):
        # A cleared cap still opens a window, so a paused budget campaign can
        # resume: the adapter compares the window to its spend file.
        svc = self.init(budget_seconds=60)
        svc.set_budget(clear=True)
        self.assertNotIn("budget", self.config())
        self.assertGreater(self.config().get("budget_started_at", 0), 0)


class BudgetFileTests(BudgetCase):
    def test_the_spend_file_round_trips(self):
        self.init(budget_seconds=60)
        path = campaign.budget_path(self.root, "feat/x")
        self.assertTrue(path.name.startswith("feat--x"))
        campaign.write_budget(self.root, "feat/x", {"active_seconds": 12.5, "spent_usd": 1.5})
        loaded = campaign.load_budget(self.root, "feat/x")
        self.assertEqual(loaded["active_seconds"], 12.5)

    def test_sessions_lists_a_budget_stop(self):
        svc = self.init(budget_seconds=60)
        campaign.write_session(
            self.root,
            "feat/x",
            {"campaign": "budget", "feature_branch": "feat/x", "status": "suspended", "reason": "budget"},
        )
        entries = svc.sessions()["sessions"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["reason"], "budget")
        self.assertEqual(entries[0]["status"], "suspended")


class CliBudgetFlagTests(unittest.TestCase):
    def test_budget_usd_parses_as_a_number(self):
        args = build_parser().parse_args(
            _normalize_argv(
                ["start", "--design", "D.md", "--budget-seconds", "60", "--budget-usd", "12.5"]
            )
        )
        self.assertEqual(args.budget_seconds, 60)
        self.assertEqual(args.budget_usd, 12.5)

    def test_budget_action_parses(self):
        args = build_parser().parse_args(
            _normalize_argv(["budget", "--seconds", "90", "--clear"])
        )
        self.assertEqual(args.seconds, 90)
        self.assertTrue(args.clear)


class CleanupTests(BudgetCase):
    def test_remove_campaign_artifacts_removes_the_spend_file(self):
        svc = self.init(budget_seconds=60)
        spend = campaign.budget_path(self.root, "feat/x")
        write_json(spend, {"active_seconds": 1.0})
        removed = svc.remove_campaign_artifacts()
        self.assertIn(str(spend), removed)
        self.assertFalse(spend.exists())
