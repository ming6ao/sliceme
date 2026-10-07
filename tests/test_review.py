"""Local review: campaign approval, comments, replies, routing, and the server.

These pin the contract from ``docs/review.md``:

* two additive tables hold comments and append-only decisions;
* one campaign-level approval covers the whole commit set;
* delivery refuses until the campaign is approved and every delivered comment
  is addressed;
* a reply is a comment row with a parent id and an optional addressing commit;
* `--resolve` routes a comment to a node or to no node;
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

    def test_comment_columns_migrate_additively(self):
        # An older plane has a comments table without the reply columns.
        with sqlite3.connect(str(db_path(self.root))) as conn:
            conn.execute("DROP TABLE IF EXISTS comments")
            conn.execute(
                "CREATE TABLE comments (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " branch_key TEXT NOT NULL, body TEXT NOT NULL, status TEXT,"
                " created_at REAL NOT NULL, addressed_at REAL)"
            )
            conn.commit()
        with Store(self.root) as store:
            columns = {
                row["name"]
                for row in store.conn.execute(
                    "PRAGMA table_info(comments)"
                ).fetchall()
            }
        self.assertIn("parent_comment_id", columns)
        self.assertIn("addressing_commit", columns)

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
        self.assertEqual(polled["pending"], [])
        acked = self.svc.review_ack(int(comment["id"]))
        self.assertEqual(acked["status"], "delivered")
        polled = self.svc.review_poll()
        self.assertEqual(polled["comments"], [])
        # A delivered comment is pending until the addressing pass answers it.
        self.assertEqual([c["id"] for c in polled["pending"]], [comment["id"]])

    def test_comment_needs_a_body(self):
        with self.assertRaises(SlicemeError):
            self.svc.review_comment(body="   ")

    def test_a_reply_row_records_parent_and_addressing_commit(self):
        root = self.svc.review_comment(body="root", file="src/a/x.py")
        reply = self.svc.review_reply(
            parent_comment_id=int(root["id"]),
            body="fixed in this commit",
            addressing_commit="deadbeef",
        )
        self.assertEqual(reply["status"], "addressed")
        self.assertEqual(reply["parent_comment_id"], root["id"])
        self.assertEqual(reply["addressing_commit"], "deadbeef")
        # The reply never changes the parent status.
        self.assertEqual(self.svc.store.get_comment(int(root["id"]))["status"], "open")

    def test_comment_with_a_parent_creates_a_reply(self):
        root = self.svc.review_comment(body="root", file="src/a/x.py")
        reply = self.svc.review_comment(
            body="a conversation turn", parent_comment_id=int(root["id"])
        )
        self.assertEqual(reply["parent_comment_id"], root["id"])
        self.assertIsNone(reply["addressing_commit"])
        comments = self.svc.review_snapshot()["comments"]
        self.assertIn(int(reply["id"]), [c["id"] for c in comments])

    def test_mark_addressed_sets_status_time_and_commit(self):
        root = self.svc.review_comment(body="root", file="src/a/x.py")
        self.svc.review_ack(int(root["id"]))
        updated = self.svc.review_mark_addressed(
            int(root["id"]), addressing_commit="cafef00d"
        )
        self.assertEqual(updated["status"], "addressed")
        self.assertIsNotNone(updated["addressed_at"])
        self.assertEqual(updated["addressing_commit"], "cafef00d")
        reply = self.svc.review_reply(
            parent_comment_id=int(root["id"]), body="done", addressing_commit="cafef00d"
        )
        # Only a root comment can be marked addressed.
        with self.assertRaises(SlicemeError):
            self.svc.review_mark_addressed(int(reply["id"]))


class ResolveTests(ReviewCase):
    def test_explicit_node_wins(self):
        comment = self.svc.review_comment(body="x", file="src/a/x.py", node="w3")
        self.assertEqual(
            self.svc.review_resolve(int(comment["id"])),
            {"comment": comment["id"], "node": "w3", "reason": "explicit"},
        )

    def test_owns_routes_to_the_longest_owned_directory(self):
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "review",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": 3,
                "nodes": [
                    {"id": "wide", "owns": ["dir:src"], "depends_on": [], "acceptance": ["true"]},
                    {"id": "deep", "owns": ["dir:src/a"], "depends_on": [], "acceptance": ["true"]},
                ],
            },
        )
        comment = self.svc.review_comment(body="x", file="src/a/x.py")
        resolved = self.svc.review_resolve(int(comment["id"]))
        self.assertEqual(resolved["node"], "deep")
        self.assertEqual(resolved["reason"], "owns")

    def test_general_comment_has_no_node(self):
        comment = self.svc.review_comment(body="restructure everything")
        self.assertEqual(
            self.svc.review_resolve(int(comment["id"])),
            {"comment": comment["id"], "node": None, "reason": "general"},
        )

    def test_outside_owns_has_no_node(self):
        comment = self.svc.review_comment(body="x", file="docs/readme.md")
        self.assertEqual(
            self.svc.review_resolve(int(comment["id"])),
            {"comment": comment["id"], "node": None, "reason": "outside_owns"},
        )

    def test_unknown_comment_refuses(self):
        with self.assertRaises(SlicemeError):
            self.svc.review_resolve(9999)


class ApprovalTests(ReviewCase):
    def test_deliver_refuses_without_approval(self):
        self.record_wave0()
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver()
        self.assertIn("not-approved", str(ctx.exception))

    def test_delivery_gates_carry_reason_codes(self):
        # The engine reports a machine-readable code per human gate so a caller
        # never parses the error text.
        self.record_wave0()
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver()
        self.assertEqual(ctx.exception.reason, "not_approved")
        self.approve_all()
        comment = self.svc.review_comment(body="please fix", file="src/a/x.py")
        self.svc.review_ack(int(comment["id"]))
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver()
        self.assertEqual(ctx.exception.reason, "unaddressed_comments")

    def test_one_approval_covers_the_whole_campaign(self):
        self.record_wave0()
        commits = [c["hash"] for c in self.svc.review_snapshot()["commits"]]
        self.assertGreaterEqual(len(commits), 2)
        decision = self.svc.review_decision(action="approve", actor="test")
        # The decision is campaign-level: it does not bind to one commit.
        self.assertIsNone(decision["commit_hash"])
        self.assertTrue(self.svc.review_snapshot()["all_approved"])
        self.assertEqual(self.svc.unapproved_commits(), [])

    def test_approve_all_admits_delivery_and_consumes(self):
        self.record_wave0()
        self.approve_all()
        self.assertTrue(self.svc.review_poll()["campaign_approved"])
        delivered = self.svc.deliver()
        self.assertEqual([r["status"] for r in delivered["results"]], ["landed"])
        # The landed merge consumes the campaign approval, so re-admission
        # refuses until the reviewer approves the campaign again.
        decision = self.svc.campaign_decision()
        self.assertIsNotNone(decision)
        self.assertIsNotNone(decision["consumed_at"])
        self.assertFalse(self.svc.campaign_approved())
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.require_all_approved()
        self.assertIn("not-approved", str(ctx.exception))

    def test_a_delivered_comment_blocks_delivery(self):
        self.record_wave0()
        self.approve_all()
        comment = self.svc.review_comment(body="please fix", file="src/a/x.py")
        self.svc.review_ack(int(comment["id"]))
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver()
        self.assertIn("unaddressed-comments", str(ctx.exception))

    def test_addressing_a_comment_reopens_the_gate(self):
        self.record_wave0()
        self.approve_all()
        comment = self.svc.review_comment(body="please fix", file="src/a/x.py")
        self.svc.review_ack(int(comment["id"]))
        self.svc.review_reply(
            parent_comment_id=int(comment["id"]),
            body="addressed in abc1234",
            addressing_commit="abc1234",
        )
        self.svc.review_mark_addressed(int(comment["id"]), addressing_commit="abc1234")
        # The addressing commit changed the diff, so the approval is stale.
        self.assertFalse(self.svc.review_snapshot()["all_approved"])
        self.assertFalse(self.svc.review_poll()["campaign_approved"])
        self.assertEqual(self.svc.review_poll()["pending"], [])
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver()
        self.assertIn("not-approved", str(ctx.exception))

    def test_a_reply_only_turn_keeps_the_approval(self):
        self.record_wave0()
        self.approve_all()
        comment = self.svc.review_comment(body="please explain", file="src/a/x.py")
        self.svc.review_ack(int(comment["id"]))
        # A reply-only turn answers in text and changes no commit.
        self.svc.review_reply(parent_comment_id=int(comment["id"]), body="see the design")
        self.svc.review_mark_addressed(int(comment["id"]))
        # The reviewed diff did not change, so the approval still covers it.
        self.assertTrue(self.svc.review_poll()["campaign_approved"])
        self.assertEqual(self.svc.review_poll()["pending"], [])
        delivered = self.svc.deliver()
        self.assertEqual([r["status"] for r in delivered["results"]], ["landed"])

    def test_request_changes_supersedes_an_approval(self):
        self.record_wave0()
        self.svc.review_decision(action="approve", actor="test")
        self.svc.review_decision(action="request_changes", note="not yet")
        self.assertFalse(self.svc.campaign_approved())
        self.assertTrue(self.svc.unapproved_commits())

    def test_request_changes_needs_a_note_or_open_comment(self):
        with self.assertRaises(SlicemeError):
            self.svc.review_decision(action="request_changes")
        self.svc.review_comment(body="fix this")
        self.svc.review_decision(action="request_changes")

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


class SurfaceDispatchTests(ReviewCase):
    def test_dispatch_routes_the_new_review_flags(self):
        comment = self.svc.review_comment(body="please fix", file="src/a/x.py")
        resolved = surface.dispatch(
            self.svc, "review", {"resolve": True, "comment_id": comment["id"]}
        )
        self.assertEqual(resolved["node"], "w1")
        reply = surface.dispatch(
            self.svc,
            "review",
            {
                "reply": True,
                "comment_id": comment["id"],
                "body": "done",
                "addressing_commit": "abc1234",
            },
        )
        self.assertEqual(reply["parent_comment_id"], comment["id"])
        self.assertEqual(reply["addressing_commit"], "abc1234")
        addressed = surface.dispatch(
            self.svc,
            "review",
            {
                "addressed": True,
                "comment_id": comment["id"],
                "addressing_commit": "abc1234",
            },
        )
        self.assertEqual(addressed["status"], "addressed")
        self.assertEqual(addressed["addressing_commit"], "abc1234")

    def test_comment_with_a_parent_creates_a_reply(self):
        root = self.svc.review_comment(body="root", file="src/a/x.py")
        reply = surface.dispatch(
            self.svc,
            "review",
            {"comment": True, "body": "a turn", "parent_comment_id": root["id"]},
        )
        self.assertEqual(reply["parent_comment_id"], root["id"])


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

    def test_server_accepts_reply_and_addressed(self):
        comment = self.svc.review_comment(body="please fix", file="src/a/x.py")
        server, thread = self._start()
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        try:
            with self._request(
                "POST",
                f"{base}/api/action",
                {
                    "action": "reply",
                    "params": {
                        "comment_id": comment["id"],
                        "body": "fixed",
                        "addressing_commit": "abc1234",
                    },
                },
                token=server.token,
            ) as response:
                result = json.loads(response.read())
            self.assertTrue(result["ok"])
            self.assertEqual(result["result"]["parent_comment_id"], comment["id"])
            with self._request(
                "POST",
                f"{base}/api/action",
                {
                    "action": "addressed",
                    "params": {
                        "comment_id": comment["id"],
                        "addressing_commit": "abc1234",
                    },
                },
                token=server.token,
            ) as response:
                result = json.loads(response.read())
            self.assertTrue(result["ok"])
            self.assertEqual(result["result"]["status"], "addressed")
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
