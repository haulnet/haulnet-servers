#!/usr/bin/env python3
"""haulnet-monitor: checks every server of the Haulnet registry and publishes live status.

Usage:
  python3 monitor/haulnet_monitor.py --db PATH [--listen 127.0.0.1:8080] [--list URL]
                                     [--interval 60] [--location TEXT] [--allow-private]
                                     [--issues OWNER/REPO]   (token in GITHUB_TOKEN; dry run without, see notify.py)

Reads the published server list, asks every server's haulnet-server status endpoint once per interval,
keeps the results in SQLite and answers over HTTP:

  GET /v1/status.json         state, versions, load and uptime of every listed server
  GET /v1/history/<id>.json   daily totals of one server for the last 30 days
  GET /healthz                "ok" while checks run on time

Standard library only. `--allow-private` lets the monitor contact private and loopback addresses; it exists
for local tests and must never be used on a public monitor.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LIST_URL = "https://servers.haulnet.cc/v1/servers.json"
USER_AGENT = "haulnet-monitor (+https://github.com/haulnet/haulnet-servers)"
INTERVAL = 60.0  # seconds between check rounds
LIST_REFRESH = 300.0  # seconds between server list downloads
TIMEOUT = 5.0  # per check: connect, request and answer together
MAX_BYTES = 64 * 1024
OFFLINE_AFTER = 3  # consecutive failed checks before a server counts as offline
RETENTION_DAYS = 90
HISTORY_DAYS = 30
DEFAULT_STATUS_PORT = 27080
LUXON_WEB_PORT = 5088  # LuxonServer's own web API: never contacted
STATUS_SCHEMA_VERSION = 1
SERVER_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
UPTIME_WINDOWS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}

log = logging.getLogger("haulnet-monitor")


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- server list -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    id: str
    display_name: str
    host: str
    status_port: int
    operator_github: str | None = None  # account the operator issues mention


@dataclass
class ServerList:
    targets: list[Target]
    compatibility: list[str]
    source_commit: str | None
    updated_at: str | None


def _int_in(value, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def parse_list(data) -> ServerList:
    """Accepts the published list (schema version 1); skips malformed servers instead of failing."""
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("not a version 1 server list")
    targets: list[Target] = []
    seen: set[str] = set()
    for item in data.get("servers") or []:
        if not isinstance(item, dict):
            continue
        sid, host = item.get("id"), item.get("host")
        port = item.get("status_port", DEFAULT_STATUS_PORT)
        name = item.get("display_name") if isinstance(item.get("display_name"), str) else sid
        if not (isinstance(sid, str) and len(sid) <= 80 and SERVER_ID.match(sid)) or sid in seen:
            continue
        if not (isinstance(host, str) and 1 <= len(host) <= 253) or not _int_in(port, 1, 65535):
            continue
        if port == LUXON_WEB_PORT:
            continue
        seen.add(sid)
        github = (item.get("operator") or {}).get("github") if isinstance(item.get("operator"), dict) else None
        targets.append(Target(sid, name[:80], host, port, github if isinstance(github, str) else None))
    luxon = (data.get("compatibility") or {}).get("luxon") or []
    compatibility = [v for v in luxon if isinstance(v, str)]
    commit = data.get("source_commit") if isinstance(data.get("source_commit"), str) else None
    updated = data.get("updated_at") if isinstance(data.get("updated_at"), str) else None
    return ServerList(targets, compatibility, commit, updated)


# --- one check ---------------------------------------------------------------------------------------------


class ProbeError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def resolve_public(host: str, allow_private: bool = False) -> str:
    """Returns the IPv4 address to contact. Resolved on every check: a DNS change after the merge must not
    point the monitor at itself or at a private network."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        candidates = [literal]
    else:
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError) as exc:
            raise ProbeError("dns", str(exc)) from None
        candidates = [ipaddress.ip_address(info[4][0]) for info in infos]
    for address in candidates:
        if address.version == 4 and (address.is_global or allow_private):
            return str(address)
    if candidates and all(a.version == 6 for a in candidates):
        raise ProbeError("no_ipv4", "the monitor checks over IPv4 only")
    raise ProbeError("not_public", "no public IPv4 address")


def parse_status(data) -> dict:
    """Keeps only typed, bounded fields of a haulnet-server /status answer."""
    if not isinstance(data, dict):
        raise ProbeError("bad_answer", "not a JSON object")
    out: dict = {}
    for key in ("haulnet_server", "luxon"):
        value = data.get(key)
        if not (isinstance(value, str) and 1 <= len(value) <= 32 and value.isprintable()):
            raise ProbeError("bad_answer", f"{key} missing or invalid")
        out[key] = value
    for key in ("rooms", "players"):
        if not _int_in(data.get(key), 0, 1_000_000):
            raise ProbeError("bad_answer", f"{key} missing or invalid")
        out[key] = data[key]
    if type(data.get("accepting")) is not bool:
        raise ProbeError("bad_answer", "accepting missing or invalid")
    out["accepting"] = data["accepting"]
    for key in ("max_rooms", "max_players", "uptime_s"):
        if _int_in(data.get(key), 0, 10**10):
            out[key] = data[key]
    if type(data.get("draining")) is bool:
        out["draining"] = data["draining"]
    return out


def fetch_status(address: str, target: Target, timeout: float = TIMEOUT) -> tuple[dict, float]:
    """GET /status from a resolved address. Returns (typed answer, TCP connect time in ms)."""
    deadline = time.monotonic() + timeout
    host_header = f"{target.host}:{target.status_port}"
    if ":" in target.host:
        host_header = f"[{target.host}]:{target.status_port}"
    conn = http.client.HTTPConnection(address, target.status_port, timeout=timeout)
    try:
        started = time.monotonic()
        try:
            conn.connect()
        except TimeoutError:
            raise ProbeError("timeout", "connect") from None
        except ConnectionRefusedError:
            raise ProbeError("refused") from None
        except OSError as exc:
            raise ProbeError("unreachable", str(exc)) from None
        connect_ms = (time.monotonic() - started) * 1000
        try:
            conn.request("GET", "/status", headers={
                "Host": host_header, "User-Agent": USER_AGENT, "Accept": "application/json"})
            response = conn.getresponse()
            if response.status == 503:
                raise ProbeError("http_503", "LuxonServer is not running")
            if response.status != 200:
                raise ProbeError("http_error", f"HTTP {response.status}")
            body = b""
            while len(body) <= MAX_BYTES:
                if time.monotonic() > deadline:
                    raise ProbeError("timeout", "answer")
                chunk = response.read(8192)
                if not chunk:
                    break
                body += chunk
        except TimeoutError:
            raise ProbeError("timeout", "answer") from None
        except (OSError, http.client.HTTPException) as exc:
            raise ProbeError("unreachable", str(exc)) from None
        if len(body) > MAX_BYTES:
            raise ProbeError("too_large")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProbeError("bad_answer", "not JSON") from None
        return parse_status(data), round(connect_ms, 1)
    finally:
        conn.close()


@dataclass
class CheckResult:
    server_id: str
    ts: int
    ok: bool
    reason: str | None = None
    connect_ms: float | None = None
    status: dict | None = None


def check(target: Target, allow_private: bool = False, timeout: float = TIMEOUT) -> CheckResult:
    ts = int(time.time())
    try:
        address = resolve_public(target.host, allow_private)
        status, connect_ms = fetch_status(address, target, timeout)
    except ProbeError as exc:
        return CheckResult(target.id, ts, False, exc.reason)
    return CheckResult(target.id, ts, True, None, connect_ms, status)


# --- state -------------------------------------------------------------------------------------------------


@dataclass
class ServerState:
    state: str = "pending"  # pending | online | offline
    since: int | None = None
    failures: int = 0
    last_ok: int | None = None
    first_failure: int | None = None  # start of the current run of failed checks

    def apply(self, result: CheckResult, offline_after: int = OFFLINE_AFTER) -> str | None:
        """Updates the state; returns the previous state when it changed."""
        before = self.state
        if result.ok:
            self.failures = 0
            self.last_ok = result.ts
            if self.state != "online":
                self.state, self.since = "online", result.ts
        else:
            self.failures += 1
            if self.failures == 1:
                self.first_failure = result.ts
            if self.failures >= offline_after and self.state != "offline":
                self.state, self.since = "offline", self.first_failure or result.ts
        return before if self.state != before else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    server_id TEXT NOT NULL, ts INTEGER NOT NULL, ok INTEGER NOT NULL, reason TEXT, connect_ms REAL,
    haulnet_server TEXT, luxon TEXT, rooms INTEGER, players INTEGER, max_rooms INTEGER, max_players INTEGER,
    accepting INTEGER, draining INTEGER, uptime_s INTEGER);
CREATE INDEX IF NOT EXISTS checks_server_ts ON checks (server_id, ts);
CREATE TABLE IF NOT EXISTS current (
    server_id TEXT PRIMARY KEY, state TEXT NOT NULL, since INTEGER, failures INTEGER NOT NULL,
    first_failure INTEGER, last_ok INTEGER);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

STATUS_COLUMNS = ("haulnet_server", "luxon", "rooms", "players", "max_rooms", "max_players", "accepting",
                  "draining", "uptime_s")


def connect_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    return db


class Monitor:
    def __init__(self, db_path: str, list_url: str = LIST_URL, interval: float = INTERVAL,
                 location: str | None = None, allow_private: bool = False, timeout: float = TIMEOUT):
        self.db_path = db_path
        self.list_url = list_url
        self.interval = interval
        self.location = location
        self.allow_private = allow_private
        self.timeout = timeout
        self.db = connect_db(db_path)
        self.lock = threading.Lock()
        self.server_list: ServerList | None = None
        self.list_fetched_at: float | None = None
        self.etag: str | None = None
        self.states: dict[str, ServerState] = {}
        self.last_round: float | None = None
        self.status_body = b""
        self.history_cache: dict[str, tuple[float, bytes]] = {}
        self.notifier = None  # notify.Notifier when operator issues are enabled
        self._load()
        self._render()

    def views(self) -> list:
        """Per-server state for the notifier, after a round."""
        from notify import ServerView
        out = []
        with self.lock:
            listing = self.server_list
            for t in (listing.targets if listing else []):
                state = self.states.get(t.id, ServerState())
                last = self._last_check(t.id)
                answer = self._last_answer(t.id)
                compatible = None if answer is None else answer["luxon"] in listing.compatibility
                out.append(ServerView(t.id, t.display_name, t.host, t.status_port, t.operator_github, state.state,
                                      state.since, last[2] if last and not last[1] else None, answer, compatible))
        return out

    def notify(self) -> None:
        if self.notifier is None or self.server_list is None:
            return
        try:
            self.notifier.evaluate(self.views(), self.server_list.compatibility)
        except Exception:  # notifications must never stop the checks
            log.exception("operator notifications failed")

    # Persistence of the last good list and the current states survives restarts.
    def _load(self) -> None:
        row = self.db.execute("SELECT value FROM meta WHERE key = 'list'").fetchone()
        if row:
            saved = json.loads(row[0])
            try:
                self.server_list = parse_list(saved["data"])
                self.etag = saved.get("etag")
                self.list_fetched_at = saved.get("fetched_at")
            except (ValueError, KeyError, TypeError):
                self.server_list = None
        for sid, state, since, failures, first_failure, last_ok in self.db.execute(
                "SELECT server_id, state, since, failures, first_failure, last_ok FROM current"):
            self.states[sid] = ServerState(state, since, failures, last_ok, first_failure)

    def refresh_list(self) -> None:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.etag and self.server_list is not None:
            headers["If-None-Match"] = self.etag
        request = urllib.request.Request(self.list_url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(4 * 1024 * 1024)
                etag = response.headers.get("ETag")
            data = json.loads(raw.decode("utf-8"))
            parsed = parse_list(data)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                self.list_fetched_at = time.time()
                return
            log.warning("server list: HTTP %s from %s; keeping the last good copy", exc.code, self.list_url)
            return
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            log.warning("server list: %s; keeping the last good copy", exc)
            return
        now = time.time()
        with self.lock:
            self.server_list, self.etag, self.list_fetched_at = parsed, etag, now
            self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('list', ?)",
                            (json.dumps({"data": data, "etag": etag, "fetched_at": now}),))
            self.db.commit()
        log.info("server list: %d server(s), commit %s", len(parsed.targets), (parsed.source_commit or "?")[:12])

    def run_round(self) -> None:
        listing = self.server_list
        if listing is None:
            return
        targets = listing.targets
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda t: check(t, self.allow_private, self.timeout), targets))
        self.record(results)

    def record(self, results: list[CheckResult]) -> None:
        with self.lock:
            for r in results:
                s = r.status or {}
                self.db.execute(
                    "INSERT INTO checks (server_id, ts, ok, reason, connect_ms, haulnet_server, luxon, rooms, players, "
                    "max_rooms, max_players, accepting, draining, uptime_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r.server_id, r.ts, int(r.ok), r.reason, r.connect_ms, *(s.get(c) for c in STATUS_COLUMNS)))
                state = self.states.setdefault(r.server_id, ServerState())
                previous = state.apply(r)
                if previous is not None:
                    log.info("%s: %s -> %s%s", r.server_id, previous, state.state, f" ({r.reason})" if r.reason else "")
                self.db.execute(
                    "INSERT OR REPLACE INTO current (server_id, state, since, failures, first_failure, last_ok) "
                    "VALUES (?,?,?,?,?,?)",
                    (r.server_id, state.state, state.since, state.failures, state.first_failure, state.last_ok))
            self.db.commit()
            self.last_round = time.time()
            self.history_cache.clear()
        self._render()

    def purge(self) -> None:
        with self.lock:
            cutoff = int(time.time()) - RETENTION_DAYS * 86400
            self.db.execute("DELETE FROM checks WHERE ts < ?", (cutoff,))
            self.db.commit()

    # --- published documents ---

    def _last_check(self, sid: str):
        return self.db.execute(
            "SELECT ts, ok, reason, connect_ms FROM checks WHERE server_id = ? ORDER BY ts DESC LIMIT 1",
            (sid,)).fetchone()

    def _last_answer(self, sid: str) -> dict | None:
        row = self.db.execute(
            "SELECT " + ", ".join(STATUS_COLUMNS) + " FROM checks WHERE server_id = ? AND ok = 1 "
            "ORDER BY ts DESC LIMIT 1", (sid,)).fetchone()
        if not row:
            return None
        answer = {}
        for key, value in zip(STATUS_COLUMNS, row):
            if value is None:
                continue
            answer[key] = bool(value) if key in ("accepting", "draining") else value
        return answer

    def _uptime(self, sid: str, now: int) -> dict:
        out = {}
        for name, seconds in UPTIME_WINDOWS.items():
            total, good = self.db.execute(
                "SELECT count(*), coalesce(sum(ok), 0) FROM checks WHERE server_id = ? AND ts >= ?",
                (sid, now - seconds)).fetchone()
            out[name] = round(good / total, 4) if total else None
        return out

    def _render(self) -> None:
        now = int(time.time())
        with self.lock:
            listing = self.server_list
            servers = []
            for target in (listing.targets if listing else []):
                state = self.states.get(target.id, ServerState())
                last = self._last_check(target.id)
                answer = self._last_answer(target.id)
                compatible = None
                if answer is not None and listing is not None:
                    compatible = answer["luxon"] in listing.compatibility
                servers.append({
                    "id": target.id,
                    "state": state.state,
                    "since": iso(state.since),
                    "last_check": iso(last[0]) if last else None,
                    "last_ok": iso(state.last_ok),
                    "last_error": last[2] if last and not last[1] else None,
                    "connect_ms": last[3] if last and last[1] else None,
                    "compatible": compatible,
                    "status": answer,
                    "uptime": self._uptime(target.id, now),
                })
            document = {
                "schema_version": STATUS_SCHEMA_VERSION,
                "generated_at": iso(now),
                "monitor": {"location": self.location, "interval_s": int(self.interval),
                            "offline_after": OFFLINE_AFTER, "address_family": "IPv4"},
                "list": {"source_commit": listing.source_commit if listing else None,
                         "updated_at": listing.updated_at if listing else None,
                         "fetched_at": iso(self.list_fetched_at)},
                "compatibility": {"luxon": listing.compatibility if listing else []},
                "servers": servers,
            }
            self.status_body = (json.dumps(document, indent=1, ensure_ascii=False) + "\n").encode("utf-8")

    def history(self, sid: str) -> bytes | None:
        listing = self.server_list
        if listing is None or sid not in {t.id for t in listing.targets}:
            return None
        with self.lock:
            cached = self.history_cache.get(sid)
            if cached and time.time() - cached[0] < 60:
                return cached[1]
            since = (int(time.time()) // 86400 - (HISTORY_DAYS - 1)) * 86400
            rows = self.db.execute(
                "SELECT ts / 86400 AS day, count(*), sum(ok), max(players), max(rooms), "
                "avg(CASE WHEN ok = 1 THEN connect_ms END) FROM checks WHERE server_id = ? AND ts >= ? "
                "GROUP BY day ORDER BY day", (sid, since)).fetchall()
            days = [{
                "date": datetime.fromtimestamp(day * 86400, timezone.utc).strftime("%Y-%m-%d"),
                "checks": total, "ok": good, "players_max": players, "rooms_max": rooms,
                "connect_ms_avg": round(connect, 1) if connect is not None else None,
            } for day, total, good, players, rooms, connect in rows]
            body = (json.dumps({"schema_version": STATUS_SCHEMA_VERSION, "id": sid, "days": days},
                               indent=1) + "\n").encode("utf-8")
            self.history_cache[sid] = (time.time(), body)
            return body

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def healthy(self) -> bool:
        return self.last_round is not None and time.time() - self.last_round < 3 * self.interval + 30


# --- HTTP --------------------------------------------------------------------------------------------------


def make_handler(monitor: Monitor):
    class Handler(BaseHTTPRequestHandler):
        server_version = "haulnet-monitor"
        sys_version = ""

        def log_message(self, format, *args):  # no access log: visitors' addresses are not kept
            pass

        def _send(self, code: int, body: bytes, content_type: str, max_age: int, head: bool) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", f"public, max-age={max_age}" if code == 200 else "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _route(self, head: bool) -> None:
            path = self.path.split("?", 1)[0]
            json_type = "application/json; charset=utf-8"
            if path == "/v1/status.json":
                self._send(200, monitor.status_body, json_type, 30, head)
            elif path.startswith("/v1/history/") and path.endswith(".json"):
                sid = path[len("/v1/history/"):-len(".json")]
                body = monitor.history(sid) if SERVER_ID.match(sid) else None
                if body is None:
                    self._send(404, b'{"error":"unknown server"}\n', json_type, 0, head)
                else:
                    self._send(200, body, json_type, 300, head)
            elif path == "/healthz":
                ok = monitor.healthy()
                self._send(200 if ok else 503, b"ok\n" if ok else b"checks are late\n",
                           "text/plain; charset=utf-8", 0, head)
            else:
                self._send(404, b"not found\n", "text/plain; charset=utf-8", 0, head)

        def do_GET(self):
            self._route(head=False)

        def do_HEAD(self):
            self._route(head=True)

    return Handler


def serve(monitor: Monitor, listen: str) -> ThreadingHTTPServer:
    host, _, port = listen.rpartition(":")
    server = ThreadingHTTPServer((host.strip("[]") or "127.0.0.1", int(port)), make_handler(monitor))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    return server


def run(monitor: Monitor, stop: threading.Event) -> None:
    next_list = next_round = time.monotonic()
    next_purge = time.monotonic() + 60
    while not stop.is_set():
        now = time.monotonic()
        if now >= next_list:
            monitor.refresh_list()
            next_list = now + LIST_REFRESH
        if now >= next_round:
            started = time.monotonic()
            try:
                monitor.run_round()
            except Exception:  # keep checking; one broken round must not stop the monitor
                log.exception("check round failed")
            monitor.notify()
            next_round = started + monitor.interval
        if now >= next_purge:
            monitor.purge()
            next_purge = now + 86400
        stop.wait(max(0.5, min(next_round, next_list) - time.monotonic()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Haulnet registry monitor")
    parser.add_argument("--db", required=True, help="SQLite database file")
    parser.add_argument("--listen", default="127.0.0.1:8080", help="HTTP address (default 127.0.0.1:8080)")
    parser.add_argument("--list", default=LIST_URL, help="server list URL")
    parser.add_argument("--interval", type=float, default=INTERVAL, help="seconds between check rounds")
    parser.add_argument("--location", help="where the monitor runs, shown on the page (e.g. 'Moscow, RU')")
    parser.add_argument("--allow-private", action="store_true", help="local tests only")
    parser.add_argument("--issues", metavar="OWNER/REPO",
                        help="open issues for operators in this repository; token from GITHUB_TOKEN, dry run without")
    parser.add_argument("--github-api", default="https://api.github.com", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    monitor = Monitor(args.db, args.list, max(5.0, args.interval), args.location, args.allow_private)
    if args.allow_private:
        log.warning("--allow-private: private and loopback addresses will be contacted (tests only)")
    if args.issues:
        from notify import Notifier
        token = os.environ.get("GITHUB_TOKEN") or None
        monitor.notifier = Notifier(args.db, args.issues, token, api=args.github_api, location=args.location)
        log.info("operator issues in %s%s", args.issues, "" if token else " (dry run: no GITHUB_TOKEN)")
    server = serve(monitor, args.listen)
    log.info("listening on %s, checking every %ds", args.listen, int(monitor.interval))

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run(monitor, stop)
    server.shutdown()
    monitor.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
