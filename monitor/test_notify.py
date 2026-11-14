"""Tests for notify.py against a fake GitHub API on loopback. Run: python -m unittest discover -s monitor -v"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify  # noqa: E402

T0 = 1_800_000_000
COMPAT = ["v1.5"]


class FakeGitHub:
    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.open_issues: list[dict] = []
        self.release = {"tag_name": "v0.1.1", "published_at": "2026-11-14T10:18:06Z"}
        self.fail = False
        self.next_number = 100
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length)) if length else None
                fake.requests.append((method, self.path, payload))
                if fake.fail:
                    return self._reply(500, {"message": "boom"})
                if self.path.endswith("/releases/latest"):
                    return self._reply(200, fake.release)
                if method == "GET" and "/issues?" in self.path:
                    return self._reply(200, fake.open_issues)
                if method == "POST" and self.path.endswith("/issues"):
                    fake.next_number += 1
                    return self._reply(201, {"number": fake.next_number})
                return self._reply(200, {})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PATCH(self):
                self._handle("PATCH")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def writes(self):
        return [(m, p, b) for m, p, b in self.requests if m != "GET"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def view(state="online", since=T0, luxon="v1.5", version="0.1.1", compatible=True, sid="europe-moscow-1",
         error=None):
    answer = {"haulnet_server": version, "luxon": luxon, "rooms": 0, "players": 0, "accepting": True}
    return notify.ServerView(sid, "Europe [Moscow] #1", "moscow-1.play.haulnet.cc", 27080, "example-op", state, since,
                             error, answer, compatible)


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "m.db")
        self.gh = FakeGitHub()
        self.notifiers: list[notify.Notifier] = []

    def tearDown(self):
        for n in self.notifiers:
            n.close()
        self.gh.close()
        self.tmp.cleanup()

    def make(self, token="t") -> notify.Notifier:
        n = notify.Notifier(self.db, "haulnet/haulnet-servers", token, api=self.gh.url, location="Moscow, RU")
        self.notifiers.append(n)
        return n

    def test_down_opens_after_an_hour_once_and_closes_on_recovery(self):
        n = self.make()
        n.evaluate([view("offline", T0, error="timeout")], COMPAT, now=T0 + 3599)
        self.assertEqual(self.gh.writes(), [])
        n.evaluate([view("offline", T0, error="timeout")], COMPAT, now=T0 + 3600)
        (method, path, body), = self.gh.writes()
        self.assertEqual((method, path), ("POST", "/repos/haulnet/haulnet-servers/issues"))
        self.assertIn("<!-- haulnet-monitor:europe-moscow-1:down -->", body["body"])
        self.assertIn("@example-op:", body["body"])
        self.assertIn("timeout", body["body"])
        self.assertIn("Europe [Moscow] #1", body["title"])
        self.assertEqual(body["labels"], ["server-problem"])
        n.evaluate([view("offline", T0)], COMPAT, now=T0 + 7200)
        self.assertEqual(len(self.gh.writes()), 1)
        n.evaluate([view("online", T0 + 7300)], COMPAT, now=T0 + 7300)
        methods = [(m, p) for m, p, _ in self.gh.writes()[1:]]
        self.assertEqual(methods, [("POST", "/repos/haulnet/haulnet-servers/issues/101/comments"),
                                   ("PATCH", "/repos/haulnet/haulnet-servers/issues/101")])
        self.assertEqual(self.gh.writes()[-1][2], {"state": "closed", "state_reason": "completed"})

    def test_removal_note_once_after_a_week(self):
        n = self.make()
        n.evaluate([view("offline", T0)], COMPAT, now=T0 + 3600)
        n.evaluate([view("offline", T0)], COMPAT, now=T0 + 7 * 86400)
        n.evaluate([view("offline", T0)], COMPAT, now=T0 + 8 * 86400)
        comments = [b for m, p, b in self.gh.writes() if p.endswith("/comments")]
        self.assertEqual(len(comments), 1)
        self.assertIn("7 days", comments[0]["body"])

    def test_dry_run_writes_nothing_and_real_run_starts_clean(self):
        dry = self.make(token=None)
        dry.evaluate([view("offline", T0)], COMPAT, now=T0 + 3600)
        dry.evaluate([view("online", T0 + 4000)], COMPAT, now=T0 + 4000)
        dry.evaluate([view("offline", T0 + 5000)], COMPAT, now=T0 + 9000)
        self.assertEqual(self.gh.writes(), [])
        dry.close()
        self.notifiers.remove(dry)
        real = self.make()
        real.evaluate([view("offline", T0 + 5000)], COMPAT, now=T0 + 9000)
        self.assertEqual([m for m, _, _ in self.gh.writes()], ["POST"])

    def test_existing_issue_is_adopted_after_database_loss(self):
        self.gh.open_issues = [{"number": 7, "created_at": "2026-11-14T10:00:00Z",
                                "body": "<!-- haulnet-monitor:europe-moscow-1:down -->\ntext"}]
        n = self.make()
        n.evaluate([view("offline", T0)], COMPAT, now=T0 + 3600)
        self.assertEqual(self.gh.writes(), [])
        n.evaluate([view("online", T0 + 3700)], COMPAT, now=T0 + 3700)
        self.assertEqual(self.gh.writes()[-1][1], "/repos/haulnet/haulnet-servers/issues/7")

    def test_incompatible_after_thirty_minutes(self):
        n = self.make()
        bad = view(luxon="v1.4", compatible=False)
        n.evaluate([bad], COMPAT, now=T0)
        n.evaluate([bad], COMPAT, now=T0 + 1799)
        self.assertEqual(self.gh.writes(), [])
        n.evaluate([bad], COMPAT, now=T0 + 1800)
        (_, _, body), = self.gh.writes()
        self.assertIn("unsupported LuxonServer", body["title"])
        self.assertIn("`v1.4`", body["body"])
        n.evaluate([view()], COMPAT, now=T0 + 2000)
        self.assertEqual(self.gh.writes()[-1][2]["state"], "closed")

    def test_outdated_thresholds(self):
        self.gh.release = {"tag_name": "v0.2.0", "published_at": "2026-11-20T00:00:00Z"}
        n = self.make()
        old_minor = view(version="0.1.1")
        n.evaluate([old_minor], COMPAT, now=T0)
        n.evaluate([old_minor], COMPAT, now=T0 + 2 * 86400 - 1)
        self.assertEqual(self.gh.writes(), [])
        n.evaluate([old_minor], COMPAT, now=T0 + 2 * 86400)
        self.assertIn("out of date", self.gh.writes()[0][2]["title"])
        self.assertIn("`0.2.0`", self.gh.writes()[0][2]["body"])

    def test_patch_behind_waits_two_weeks(self):
        self.gh.release = {"tag_name": "v0.1.2", "published_at": "2026-11-20T00:00:00Z"}
        n = self.make()
        n.evaluate([view(version="0.1.1")], COMPAT, now=T0)
        n.evaluate([view(version="0.1.1")], COMPAT, now=T0 + 13 * 86400)
        self.assertEqual(self.gh.writes(), [])
        n.evaluate([view(version="0.1.1")], COMPAT, now=T0 + 14 * 86400)
        self.assertEqual(len(self.gh.writes()), 1)

    def test_removed_server_closes_its_issue(self):
        n = self.make()
        n.evaluate([view("offline", T0)], COMPAT, now=T0 + 3600)
        n.evaluate([], COMPAT, now=T0 + 3700)
        self.assertEqual(self.gh.writes()[-1][2]["state"], "closed")

    def test_api_errors_back_off(self):
        n = self.make()
        self.gh.fail = True
        n.evaluate([view("offline", T0)], COMPAT, now=T0 + 3600)  # must not raise
        count = len(self.gh.requests)
        self.gh.fail = False
        n.evaluate([view("offline", T0)], COMPAT, now=T0 + 3700)
        self.assertEqual(len(self.gh.requests), count)  # still backing off
        n.evaluate([view("offline", T0)], COMPAT, now=T0 + 3600 + notify.BACKOFF)
        self.assertEqual([m for m, _, _ in self.gh.writes()], ["POST"])

    def test_invalid_operator_is_not_mentioned(self):
        n = self.make()
        v = view("offline", T0)
        v.operator_github = "bad name @everyone"
        n.evaluate([v], COMPAT, now=T0 + 3600)
        self.assertNotIn("@", self.gh.writes()[0][2]["body"].split("-->", 1)[1].split("Live status")[0])


if __name__ == "__main__":
    unittest.main()
