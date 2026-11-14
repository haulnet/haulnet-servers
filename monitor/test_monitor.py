"""Unit tests for haulnet_monitor. Network use is limited to loopback servers started by the tests.

Run: python -m unittest discover -s monitor -v
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import haulnet_monitor as hm  # noqa: E402

GOOD = {"haulnet_server": "0.1.1", "luxon": "v1.5", "name": "x", "rooms": 1, "players": 3, "max_rooms": 20,
        "max_players": 120, "cpu_busy": 0.5, "accepting": True, "draining": False, "uptime_s": 60}


class FakeStatus:
    """A loopback HTTP server whose /status answer the test controls."""

    def __init__(self, code: int = 200, body: bytes | None = None):
        self.code, self.body = code, body if body is not None else json.dumps(GOOD).encode()
        self.hosts: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fake.hosts.append(self.headers.get("Host", ""))
                self.send_response(fake.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(fake.body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listing(servers: list[dict]) -> dict:
    return {"schema_version": 1, "updated_at": "2026-11-14T00:00:00Z", "source_commit": "a" * 40,
            "compatibility": {"luxon": ["v1.5"]}, "regions": [], "cities": [], "servers": servers}


def entry(sid: str, port: int, host: str = "127.0.0.1") -> dict:
    return {"id": sid, "display_name": sid, "region": "europe", "city": "moscow", "number": 1, "host": host,
            "port": 27000, "status_port": port, "operator": {"name": "T", "github": "t"}}


class ParseTests(unittest.TestCase):
    def test_status_keeps_typed_fields_only(self):
        out = hm.parse_status(dict(GOOD, extra="<b>x</b>"))
        self.assertEqual(out["players"], 3)
        self.assertNotIn("extra", out)
        self.assertNotIn("cpu_busy", out)
        self.assertNotIn("name", out)

    def test_status_rejects_bad_types(self):
        for bad in (dict(GOOD, rooms=True), dict(GOOD, players=-1), dict(GOOD, luxon=""), dict(GOOD, luxon="x" * 33),
                    dict(GOOD, accepting=1), {k: v for k, v in GOOD.items() if k != "haulnet_server"}, [], "x"):
            with self.assertRaises(hm.ProbeError, msg=repr(bad)) as ctx:
                hm.parse_status(bad)
            self.assertEqual(ctx.exception.reason, "bad_answer")

    def test_list_skips_malformed_servers_and_luxon_web_port(self):
        data = listing([entry("europe-moscow-1", 27080), entry("bad id", 27080), {"id": "europe-x-1"},
                        entry("europe-moscow-2", 5088), entry("europe-moscow-1", 1)])
        parsed = hm.parse_list(data)
        self.assertEqual([t.id for t in parsed.targets], ["europe-moscow-1"])
        self.assertEqual(parsed.compatibility, ["v1.5"])

    def test_list_rejects_other_versions(self):
        with self.assertRaises(ValueError):
            hm.parse_list({"schema_version": 2, "servers": []})


class AddressTests(unittest.TestCase):
    def test_private_and_special_addresses_are_refused(self):
        for host in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "100.64.0.1", "169.254.1.1", "0.0.0.0"):
            with self.assertRaises(hm.ProbeError, msg=host) as ctx:
                hm.resolve_public(host)
            self.assertEqual(ctx.exception.reason, "not_public")

    def test_dns_name_pointing_at_loopback_is_refused(self):
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        with mock.patch("socket.getaddrinfo", return_value=fake):
            with self.assertRaises(hm.ProbeError) as ctx:
                hm.resolve_public("evil.example.org")
        self.assertEqual(ctx.exception.reason, "not_public")

    def test_public_address_is_used(self):
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.1.1", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("109.172.100.169", 0))]
        with mock.patch("socket.getaddrinfo", return_value=fake):
            self.assertEqual(hm.resolve_public("moscow-1.play.haulnet.cc"), "109.172.100.169")

    def test_ipv6_only_is_reported(self):
        with self.assertRaises(hm.ProbeError) as ctx:
            hm.resolve_public("2a00:b700:2::1de")
        self.assertEqual(ctx.exception.reason, "no_ipv4")

    def test_allow_private_is_needed_for_loopback(self):
        self.assertEqual(hm.resolve_public("127.0.0.1", allow_private=True), "127.0.0.1")


class StateTests(unittest.TestCase):
    def result(self, ok: bool, ts: int) -> hm.CheckResult:
        return hm.CheckResult("s", ts, ok, None if ok else "timeout")

    def test_offline_only_after_three_failures(self):
        s = hm.ServerState()
        self.assertEqual(s.apply(self.result(True, 100)), "pending")
        self.assertEqual((s.state, s.since), ("online", 100))
        self.assertIsNone(s.apply(self.result(False, 160)))
        self.assertIsNone(s.apply(self.result(False, 220)))
        self.assertEqual(s.state, "online")
        self.assertEqual(s.apply(self.result(False, 280)), "online")
        self.assertEqual((s.state, s.since), ("offline", 160))
        s.apply(self.result(True, 340))
        self.assertEqual((s.state, s.since, s.failures), ("online", 340, 0))

    def test_one_failure_between_successes_changes_nothing(self):
        s = hm.ServerState()
        s.apply(self.result(True, 0))
        s.apply(self.result(False, 60))
        s.apply(self.result(True, 120))
        self.assertEqual((s.state, s.since), ("online", 0))


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "m.db")
        self.good, self.down, self.junk = FakeStatus(), FakeStatus(503, b"no"), FakeStatus(body=b"not json")
        self.closed = free_port()
        self.monitors: list[hm.Monitor] = []
        self.data = listing([entry("europe-good-1", self.good.port), entry("europe-down-1", self.down.port),
                             entry("europe-junk-1", self.junk.port), entry("europe-closed-1", self.closed)])

    def tearDown(self):
        for f in (self.good, self.down, self.junk):
            f.close()
        for m in self.monitors:
            m.close()
        self.tmp.cleanup()

    def make(self) -> hm.Monitor:
        m = hm.Monitor(self.db, allow_private=True, timeout=2, location="Test")
        m.server_list = hm.parse_list(self.data)
        self.monitors.append(m)
        return m

    def status(self, m: hm.Monitor) -> dict:
        return {s["id"]: s for s in json.loads(m.status_body)["servers"]}

    def test_rounds_states_and_documents(self):
        m = self.make()
        m.run_round()
        first = self.status(m)
        self.assertEqual(first["europe-good-1"]["state"], "online")
        self.assertEqual(first["europe-good-1"]["status"]["players"], 3)
        self.assertTrue(first["europe-good-1"]["compatible"])
        self.assertIsNotNone(first["europe-good-1"]["connect_ms"])
        self.assertEqual(first["europe-down-1"]["state"], "pending")
        self.assertEqual(first["europe-down-1"]["last_error"], "http_503")
        self.assertEqual(first["europe-junk-1"]["last_error"], "bad_answer")
        # Linux refuses at once; Windows retries a refused loopback connect until the timeout.
        self.assertIn(first["europe-closed-1"]["last_error"], ("refused", "timeout"))
        self.assertEqual(self.good.hosts[-1], f"127.0.0.1:{self.good.port}")

        m.run_round()
        m.run_round()
        third = self.status(m)
        for sid in ("europe-down-1", "europe-junk-1", "europe-closed-1"):
            self.assertEqual(third[sid]["state"], "offline", sid)
            self.assertEqual(third[sid]["uptime"]["24h"], 0.0)
            self.assertIsNone(third[sid]["status"])
        self.assertEqual(third["europe-good-1"]["uptime"], {"24h": 1.0, "7d": 1.0, "30d": 1.0})

        history = json.loads(m.history("europe-good-1"))
        self.assertEqual(history["days"][-1]["checks"], 3)
        self.assertEqual(history["days"][-1]["players_max"], 3)
        self.assertIsNone(m.history("europe-unknown-1"))

    def test_incompatible_version_is_flagged(self):
        self.good.body = json.dumps(dict(GOOD, luxon="v1.4")).encode()
        m = self.make()
        m.run_round()
        s = self.status(m)["europe-good-1"]
        self.assertEqual(s["state"], "online")
        self.assertFalse(s["compatible"])

    def test_state_survives_restart(self):
        m = self.make()
        for _ in range(3):
            m.run_round()
        m.close()
        self.monitors.remove(m)
        again = self.make()
        again._render()
        self.assertEqual(self.status(again)["europe-down-1"]["state"], "offline")

    def test_without_allow_private_loopback_is_never_contacted(self):
        m = self.make()
        m.allow_private = False
        m.run_round()
        self.assertEqual(self.good.hosts, [])
        self.assertEqual(self.status(m)["europe-good-1"]["last_error"], "not_public")

    def test_http_api(self):
        m = self.make()
        m.run_round()
        port = free_port()
        server = hm.serve(m, f"127.0.0.1:{port}")
        try:
            base = f"http://127.0.0.1:{port}"
            with urllib.request.urlopen(base + "/v1/status.json") as r:
                self.assertEqual(r.headers["Access-Control-Allow-Origin"], "*")
                self.assertIn("application/json", r.headers["Content-Type"])
                self.assertEqual(json.load(r)["monitor"]["location"], "Test")
            with urllib.request.urlopen(base + "/v1/history/europe-good-1.json") as r:
                self.assertEqual(json.load(r)["id"], "europe-good-1")
            with urllib.request.urlopen(base + "/healthz") as r:
                self.assertEqual(r.read(), b"ok\n")
            for path in ("/v1/history/europe-unknown-1.json", "/v1/history/..%2F.json", "/", "/status"):
                with self.assertRaises(urllib.error.HTTPError, msg=path) as ctx:
                    urllib.request.urlopen(base + path)
                self.assertEqual(ctx.exception.code, 404)
                ctx.exception.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_views_for_the_notifier(self):
        self.data["servers"][0]["operator"] = {"name": "T", "github": "op-1"}
        m = self.make()
        for _ in range(3):
            m.run_round()
        views = {v.id: v for v in m.views()}
        self.assertEqual(views["europe-good-1"].operator_github, "op-1")
        self.assertEqual(views["europe-good-1"].state, "online")
        self.assertTrue(views["europe-good-1"].compatible)
        self.assertEqual(views["europe-down-1"].state, "offline")
        self.assertEqual(views["europe-down-1"].last_error, "http_503")
        self.assertIsNone(views["europe-down-1"].answer)

    def test_list_refresh_keeps_last_good_copy(self):
        m = hm.Monitor(self.db, list_url="http://127.0.0.1:%d/v1/servers.json" % self.closed, allow_private=True)
        self.monitors.append(m)
        m.server_list = hm.parse_list(self.data)
        m.refresh_list()
        self.assertEqual(len(m.server_list.targets), 4)


if __name__ == "__main__":
    unittest.main()
