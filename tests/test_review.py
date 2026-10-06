"""Local review: per-commit approvals, the report, the relay, and the server.

These pin the contract from ``docs/review.md``:

* two additive tables hold comments and append-only per-commit decisions;
* delivery refuses until every accumulated commit is approved;
* a new commit after an approval makes delivery refuse again;
* the report is included in the packet even though it is git-ignored;
* the loopback server serves a snapshot and refuses an unauthenticated write.
"""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sliceme import campaign
from sliceme import surface
from sliceme.review import diff as review_diff
from sliceme.review import security
from sliceme.review import server as review_server
from sliceme.review.server import build_server
from sliceme.service import Service
from sliceme.store import Store
from sliceme.util import SlicemeError, db_path, write_json


CLI = Path(__file__).resolve().parent.parent / "bin" / "sliceme"
FAKE_GH_BIN = Path(__file__).resolve().parent / "bin"


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class ReviewCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    nodes = [
        {"id": "w1", "owns": ["dir:src/a"], "depends_on": [], "acceptance": ["true"]},
        {"id": "w2", "owns": ["dir:src/b"], "depends_on": [], "acceptance": ["true"]},
        {"id": "w3", "owns": ["dir:src/c"], "depends_on": ["w1"], "acceptance": ["true"]},
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        for sub in ("a", "b", "c"):
            (self.root / "src" / sub).mkdir(parents=True)
        (self.root / "src" / "a" / "x.py").write_text("a = 1\n")
        (self.root / "src" / "b" / "y.py").write_text("b = 1\n")
        (self.root / "src" / "c" / "z.py").write_text("c = 1\n")
        (self.root / "README.md").write_text("# Test plane\n\nHello **markdown**.\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        run("git", "checkout", "-q", "-b", "feat/x", cwd=self.root)
        self.remote_tmp = tempfile.TemporaryDirectory()
        remote = Path(self.remote_tmp.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        run("git", "remote", "add", "origin", str(remote), cwd=self.root)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(FAKE_GH_BIN) + os.pathsep + self._old_path
        Service.init_plane(self.root, checks=self.checks)
        self.svc = Service(self.root)
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "review",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": 3,
                "nodes": self.nodes,
            },
        )

    def tearDown(self):
        self.svc.close()
        os.environ["PATH"] = self._old_path
        self.remote_tmp.cleanup()
        self.tmp.cleanup()

    def edit(self, worktree, rel, content):
        (Path(worktree) / rel).write_text(content)

    def record_wave0(self):
        unit = self.svc.create_campaign_workspace(base="feat/x")
        self.edit(unit["worktree"], "src/a/x.py", "a = 2\n")
        self.edit(unit["worktree"], "src/b/y.py", "b = 2\n")
        return self.svc.record_wave(0, messages={"w1": "test", "w2": "test"})

    def record_wave1(self):
        unit = self.svc.create_campaign_workspace(base="feat/x")
        self.edit(unit["worktree"], "src/c/z.py", "c = 2\n")
        return self.svc.record_wave(1, messages={"w3": "test"})

    def approve_all(self):
        return self.svc.review_decision(action="approve", all_commits=True, actor="test")


class SchemaTests(ReviewCase):
    def test_review_tables_exist(self):
        with Store(self.root) as store:
            names = {
                row["name"]
                for row in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertIn("review_decisions", names)
        self.assertIn("comments", names)

    def test_migration_on_an_older_plane(self):
        with sqlite3.connect(str(db_path(self.root))) as conn:
            conn.execute("DROP TABLE IF EXISTS review_decisions")
            conn.execute("DROP TABLE IF EXISTS comments")
            conn.commit()
        with Store(self.root) as store:
            names = {
                row["name"]
                for row in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertIn("review_decisions", names)
        self.assertIn("comments", names)


class CommentRelayTests(ReviewCase):
    def test_comment_poll_and_ack(self):
        comment = self.svc.review_comment(
            body="please rename this",
            commit="abc123",
            file="src/a/x.py",
            side="new",
            line=11,
            line_end=12,
        )
        self.assertEqual(comment["status"], "open")
        polled = self.svc.review_poll()
        self.assertEqual([c["id"] for c in polled["comments"]], [comment["id"]])
        acked = self.svc.review_ack(int(comment["id"]))
        self.assertEqual(acked["status"], "delivered")
        self.assertEqual(self.svc.review_poll()["comments"], [])

    def test_comment_needs_a_body(self):
        with self.assertRaises(SlicemeError):
            self.svc.review_comment(body="   ")


class ApprovalTests(ReviewCase):
    def test_deliver_refuses_without_approval(self):
        self.record_wave0()
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver()
        self.assertIn("not-approved", str(ctx.exception))

    def test_approve_one_commit_leaves_the_rest_unapproved(self):
        self.record_wave0()
        commits = [c["hash"] for c in self.svc.review_snapshot()["commits"]]
        self.assertGreaterEqual(len(commits), 2)
        self.svc.review_decision(action="approve", commit=commits[0], actor="test")
        unapproved = self.svc.unapproved_commits()
        self.assertIn(commits[1], unapproved)
        self.assertNotIn(commits[0], unapproved)

    def test_approve_all_admits_delivery_and_consumes(self):
        self.record_wave0()
        self.approve_all()
        self.assertTrue(self.svc.review_snapshot()["all_approved"])
        delivered = self.svc.deliver()
        self.assertEqual([r["status"] for r in delivered["results"]], ["landed"])
        # The landed merge consumes every approval it used.
        decisions = self.svc.store.latest_decisions_by_commit("feat--x")
        self.assertTrue(decisions)
        self.assertTrue(all(d["consumed_at"] is not None for d in decisions.values()))

    def test_a_new_commit_makes_delivery_refuse_again(self):
        self.record_wave0()
        self.approve_all()
        self.record_wave1()
        self.assertFalse(self.svc.review_snapshot()["all_approved"])
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver()
        self.assertIn("not-approved", str(ctx.exception))

    def test_request_changes_supersedes_an_approval(self):
        self.record_wave0()
        commits = [c["hash"] for c in self.svc.review_snapshot()["commits"]]
        self.svc.review_decision(action="approve", commit=commits[0], actor="test")
        self.svc.review_decision(
            action="request_changes", commit=commits[0], note="not yet"
        )
        self.assertIn(commits[0], self.svc.unapproved_commits())

    def test_request_changes_needs_a_note_or_open_comment(self):
        with self.assertRaises(SlicemeError):
            self.svc.review_decision(action="request_changes", commit="abc")
        self.svc.review_comment(body="fix this")
        self.svc.review_decision(action="request_changes", commit="abc")

    def test_override_admits_delivery_and_needs_a_note(self):
        self.record_wave0()
        with self.assertRaises(SlicemeError):
            self.svc.review_decision(action="override")
        self.svc.review_decision(action="override", note="accepted risk")
        delivered = self.svc.deliver()
        self.assertEqual([r["status"] for r in delivered["results"]], ["landed"])

    def test_failed_delivery_leaves_approvals_unconsumed(self):
        self.record_wave0()
        self.approve_all()
        config = json.loads((self.root / ".sliceme" / "config.json").read_text())
        config["checks"] = [{"name": "needs-OK", "command": "test -f OK", "required": True}]
        (self.root / ".sliceme" / "config.json").write_text(json.dumps(config))
        delivered = self.svc.deliver()
        self.assertEqual(delivered["results"][0]["status"], "failed")
        self.assertTrue(self.svc.review_snapshot()["all_approved"])


class PacketTests(ReviewCase):
    def test_packet_lists_commits_files_and_evidence(self):
        result = self.record_wave0()
        executor = self.svc.executor()
        for candidate in result["candidates"]:
            job = executor.submit(
                source=f"node:{candidate['node']}",
                commit=candidate["head_commit"],
                commands=["true"],
            )["job"]
            executor.run_job(job)
        snapshot = self.svc.review_snapshot()
        self.assertTrue(snapshot["commits"])
        self.assertFalse(snapshot["all_approved"])
        paths = {row["path"] for row in snapshot["files"]}
        self.assertIn("src/a/x.py", paths)
        hashes = {c["hash"] for c in snapshot["commits"]}
        self.assertTrue(set(snapshot["evidence"]) <= hashes)

    def test_report_is_included_even_though_git_ignored(self):
        self.record_wave0()
        self.svc.report(narrative="the human story")
        snapshot = self.svc.review_snapshot()
        self.assertTrue(snapshot["report"]["exists"])
        self.assertIn("the human story", snapshot["report"]["content"])

    def test_file_lines_are_parsed(self):
        self.record_wave0()
        result = self.svc.review_diff(None, "src/a/x.py")
        types = [line["type"] for line in result["lines"]]
        self.assertIn("add", types)

    def test_review_file_reads_the_committed_text(self):
        self.record_wave0()
        result = self.svc.review_file(None, "README.md")
        self.assertIn("markdown", result["content"])
        with self.assertRaises(SlicemeError):
            self.svc.review_file(None, "../etc/passwd")


class DiffParsingTests(unittest.TestCase):
    def test_parse_unified_diff(self):
        text = (
            "diff --git a/x b/x\n"
            "index 111..222 100644\n"
            "--- a/x\n"
            "+++ b/x\n"
            "@@ -1,3 +1,4 @@\n"
            " context\n"
            "-old\n"
            "+new\n"
            "+extra\n"
        )
        lines = review_diff.parse_unified_diff(text)
        kinds = [(line["type"], line["old"], line["new"]) for line in lines]
        self.assertIn(("context", 1, 1), kinds)
        self.assertIn(("delete", 2, None), kinds)
        self.assertIn(("add", None, 2), kinds)
        self.assertIn(("add", None, 3), kinds)


class SecurityTests(unittest.TestCase):
    def test_token_match_is_exact(self):
        token = security.mint_token()
        self.assertTrue(security.constant_time_token_match(token, token))
        self.assertFalse(security.constant_time_token_match(token, token + "x"))
        self.assertFalse(security.constant_time_token_match(token, None))

    def test_loopback_only(self):
        self.assertTrue(security.is_loopback_host("127.0.0.1:8080"))
        self.assertTrue(security.is_loopback_host("[::1]:8080"))
        self.assertTrue(security.is_loopback_host("localhost"))
        self.assertFalse(security.is_loopback_host("10.0.0.1"))
        self.assertTrue(security.is_loopback_origin("http://127.0.0.1:5000"))
        self.assertFalse(security.is_loopback_origin("https://evil.example"))


class ServerTests(ReviewCase):
    def _start(self):
        server = build_server([self.root], port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _request(self, method, url, payload=None, token=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Sliceme-Token"] = token
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        return urllib.request.urlopen(request, timeout=10)

    def test_state_diff_and_authenticated_action(self):
        self.record_wave0()
        server, thread = self._start()
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        try:
            with self._request("GET", f"{base}/api/state") as response:
                state = json.loads(response.read())
            self.assertIn("commits", state)
            self.assertFalse(state["all_approved"])
            self.assertIn("files", state)

            with self._request(
                "GET", f"{base}/api/diff?file=src/a/x.py"
            ) as response:
                diff = json.loads(response.read())
            self.assertTrue(diff["lines"])

            # A write without the token is refused.
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._request(
                    "POST",
                    f"{base}/api/action",
                    {"action": "comment", "params": {"body": "hi"}},
                )
            self.assertEqual(ctx.exception.code, 403)

            # A write with the token is accepted.
            with self._request(
                "POST",
                f"{base}/api/action",
                {"action": "comment", "params": {"body": "looks good", "file": "src/a/x.py"}},
                token=server.token,
            ) as response:
                result = json.loads(response.read())
            self.assertTrue(result["ok"])
            self.assertEqual(result["result"]["body"], "looks good")

            # Approve all, then the state shows the campaign as approved.
            with self._request(
                "POST",
                f"{base}/api/action",
                {"action": "decision", "params": {"decision": "approve", "all": True}},
                token=server.token,
            ) as response:
                self.assertTrue(json.loads(response.read())["ok"])
            with self._request("GET", f"{base}/api/state") as response:
                self.assertTrue(json.loads(response.read())["all_approved"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_static_index_is_served_with_a_strict_csp(self):
        server, thread = self._start()
        port = server.server_address[1]
        try:
            with self._request("GET", f"http://127.0.0.1:{port}/") as response:
                body = response.read().decode("utf-8")
                csp = response.headers.get("Content-Security-Policy")
            self.assertIn("sliceme review", body)
            self.assertIn("default-src 'none'", csp)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_markdown_client_is_served(self):
        server, thread = self._start()
        port = server.server_address[1]
        try:
            with self._request(
                "GET", f"http://127.0.0.1:{port}/markdown.js"
            ) as response:
                body = response.read().decode("utf-8")
                csp = response.headers.get("Content-Security-Policy")
            self.assertIn("renderMarkdown", body)
            self.assertIn("default-src 'none'", csp)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_file_route_serves_markdown_and_rejects_traversal(self):
        server, thread = self._start()
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        try:
            with self._request("GET", f"{base}/api/file?file=README.md") as response:
                body = json.loads(response.read())
            self.assertIn("markdown", body["content"])
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._request("GET", f"{base}/api/file?file=../etc/passwd")
            self.assertEqual(ctx.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_run_server_removes_the_url_file_on_an_early_exit(self):
        # A supervisor stops the server with a terminate signal. A signal that
        # arrives before ``serve_forever`` (during the browser open) must still
        # remove the URL file.
        path = self.root / "review.url"
        with (
            mock.patch.object(review_server, "open_browser", side_effect=SystemExit(0)),
            mock.patch.object(review_server, "_stdin_is_supervised", return_value=False),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                review_server.run_server([self.root], browser=True, url_file=path)
        self.assertFalse(path.exists())

    def test_run_server_serves_when_the_browser_probe_hangs(self):
        # On some hosts the browser helper (`xdg-settings`) never returns.  The
        # server must still reach `serve_forever` and serve the URL.
        path = self.root / "review.url"
        served = threading.Event()

        def stuck():
            time.sleep(5)
            raise webbrowser.Error

        def fake_serve_forever(self, poll_interval=0.5):
            served.set()

        with (
            mock.patch("sliceme.review.server.webbrowser.get", side_effect=stuck),
            mock.patch.object(review_server, "BROWSER_OPEN_TIMEOUT", 0.1),
            mock.patch.object(review_server, "_stdin_is_supervised", return_value=False),
            mock.patch.object(review_server._Server, "serve_forever", fake_serve_forever),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            review_server.run_server([self.root], browser=True, url_file=path)
        self.assertTrue(served.is_set())
        self.assertFalse(path.exists())

    def test_server_exits_when_the_parent_closes_stdin(self):
        # The coordinator spawns the server with a pipe on standard input.  A
        # parent exit must stop the server, so no orphan server survives.
        path = self.root / "review.url"
        process = subprocess.Popen(
            [
                sys.executable,
                str(CLI),
                "review",
                "--serve",
                "--no-browser",
                "--url-file",
                str(path),
            ],
            cwd=str(self.root),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not path.exists():
                time.sleep(0.1)
            self.assertTrue(path.exists(), "the server did not write its URL")
            process.stdin.close()
            process.wait(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self.assertIsNotNone(process.returncode)
        self.assertFalse(path.exists())


class MarkdownClientTests(unittest.TestCase):
    def test_client_uses_the_markdown_renderer(self):
        web = Path(__file__).resolve().parent.parent / "sliceme" / "review" / "web"
        app = (web / "app.js").read_text(encoding="utf-8")
        index = (web / "index.html").read_text(encoding="utf-8")
        self.assertIn('from "/markdown.js"', app)
        self.assertIn("renderMarkdown(els.report", app)
        self.assertIn("renderMarkdown(els.preview", app)
        self.assertIn('id="report" class="report markdown"', index)
        self.assertIn('id="preview" class="report markdown"', index)

    def test_markdown_parser_unit_test(self):
        # The parser is pure, so a Node harness checks the block and inline
        # tokens and the URL guard.
        node = shutil.which("node")
        harness = Path(__file__).resolve().parent / "markdown_test.mjs"
        if node is None or not harness.is_file():
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, str(harness)],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class BrowserAndUrlTests(unittest.TestCase):
    def test_write_url_file_is_private_and_has_no_leftovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "review.url"
            result = review_server.write_url_file(path, "http://127.0.0.1:9/#token=abc")
            self.assertEqual(result, path)
            self.assertEqual(
                path.read_text(encoding="utf-8"), "http://127.0.0.1:9/#token=abc\n"
            )
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual([p.name for p in path.parent.iterdir()], ["review.url"])

    def test_open_browser_returns_false_without_a_browser(self):
        with mock.patch(
            "sliceme.review.server.webbrowser.get", side_effect=webbrowser.Error
        ):
            self.assertFalse(review_server.open_browser("http://127.0.0.1:1/#token=x"))

    def test_open_browser_reports_the_open_result(self):
        with (
            mock.patch("sliceme.review.server.webbrowser.get", return_value=object()),
            mock.patch(
                "sliceme.review.server.webbrowser.open", return_value=True
            ) as opened,
        ):
            self.assertTrue(review_server.open_browser("http://127.0.0.1:1/#token=x"))
        opened.assert_called_once()

    def test_open_browser_swallows_a_launch_error(self):
        with (
            mock.patch("sliceme.review.server.webbrowser.get", return_value=object()),
            mock.patch(
                "sliceme.review.server.webbrowser.open", side_effect=OSError("boom")
            ),
        ):
            self.assertFalse(review_server.open_browser("http://127.0.0.1:1/#token=x"))

    def test_open_browser_times_out_a_stuck_probe(self):
        # `webbrowser.get` can block forever in `xdg-settings`.  The probe must
        # return a bounded answer so the server can start serving.
        def stuck():
            time.sleep(5)
            raise webbrowser.Error

        start = time.monotonic()
        with mock.patch("sliceme.review.server.webbrowser.get", side_effect=stuck):
            result = review_server.open_browser(
                "http://127.0.0.1:1/#token=x", timeout=0.1
            )
        elapsed = time.monotonic() - start
        self.assertFalse(result)
        self.assertLess(elapsed, 1.0)

    def test_stdin_is_supervised_accepts_a_socket_pair(self):
        # Node's stdio pipe is a socket pair, not a FIFO.  A TTY must stay open
        # for a human who runs the foreground server.
        for mode, expected in (
            (stat.S_IFSOCK, True),
            (stat.S_IFIFO, True),
            (stat.S_IFCHR, False),
        ):
            with mock.patch(
                "sliceme.review.server.os.fstat",
                return_value=SimpleNamespace(st_mode=mode | 0o600),
            ):
                self.assertEqual(review_server._stdin_is_supervised(), expected)

    def test_dispatch_forwards_browser_and_url_file(self):
        service = SimpleNamespace(root=Path("/tmp/plane"))
        captured: dict = {}

        def fake_serve(roots, **kwargs):
            captured["roots"] = roots
            captured.update(kwargs)
            return {"url": "u"}

        with mock.patch("sliceme.review.api.serve", side_effect=fake_serve):
            result = surface._dispatch_review(
                service, {"serve": True, "no_browser": True, "url_file": "/tmp/u"}
            )
        self.assertEqual(result, {"url": "u"})
        self.assertEqual(captured["roots"], [Path("/tmp/plane")])
        self.assertFalse(captured["browser"])
        self.assertEqual(captured["url_file"], "/tmp/u")

    def test_dispatch_opens_the_browser_by_default(self):
        service = SimpleNamespace(root=Path("/tmp/plane"))
        captured: dict = {}

        def fake_serve(roots, **kwargs):
            captured.update(kwargs)
            return {}

        with mock.patch("sliceme.review.api.serve", side_effect=fake_serve):
            surface._dispatch_review(service, {"serve": True})
        self.assertTrue(captured["browser"])
        self.assertIsNone(captured["url_file"])


if __name__ == "__main__":
    unittest.main()
