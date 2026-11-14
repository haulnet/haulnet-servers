#!/usr/bin/env python3
"""Validate the Haulnet server registry and build the published server list.

Usage:
  python tools/registry.py validate [--base REF] [--no-dns] [--probe]
  python tools/registry.py build --out DIR

`validate --base REF` also applies the rules for entries added or changed since REF
(the pull request base). Without it, every entry is treated as unchanged.
`--probe` asks the haulnet-server status endpoint of every added or changed entry.
"""

from __future__ import annotations

import argparse
import html
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
LOCATIONS_FILE = "data/locations.json"
COMPATIBILITY_FILE = "data/compatibility.json"
SERVERS_DIR = "servers"
PLACEHOLDER = "servers/.gitkeep"
SCHEMA_VERSION = 1
DNS_TIMEOUT = 10.0
DEFAULT_STATUS_PORT = 27080
LUXON_WEB_PORT = 5088  # LuxonServer's own web API: no authentication, must never be public
STATUS_TIMEOUT = 5.0
STATUS_ATTEMPTS = 2
STATUS_MAX_BYTES = 64 * 1024
# Live status for the web page (monitor/haulnet_monitor.py on haulnet-monitor); the page works without it.
MONITOR_STATUS_URL = "https://monitor.haulnet.cc/v1/status.json"

HOSTNAME_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
TLD = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")
SERVER_FILE = re.compile(r"^servers/([a-z-]+)/([a-z-]+)-([0-9]+)\.json$")
# Names that never reach a public server.
RESERVED_SUFFIXES = (
    "localhost", "local", "localdomain", "test", "example", "invalid", "internal", "intranet",
    "lan", "home", "corp", "private", "arpa", "onion",
    "example.com", "example.net", "example.org",
)


@dataclass
class Report:
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)

    def error(self, path: str, message: str) -> None:
        self.errors.append((path, message))

    def warn(self, path: str, message: str) -> None:
        self.warnings.append((path, message))

    def emit(self) -> None:
        github = os.environ.get("GITHUB_ACTIONS") == "true"
        for level, items in (("warning", self.warnings), ("error", self.errors)):
            for path, message in items:
                if github:
                    text = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                    print(f"::{level} file={path},title=registry::{text}")
                else:
                    print(f"{level.upper()}: {path}: {message}")


@dataclass
class Server:
    path: str
    data: dict
    region: str = ""
    region_name: str = ""
    city_name: str = ""
    status: str = "unchanged"  # unchanged | added | changed
    probe: dict | None = None  # answer of the status endpoint, when it was asked

    @property
    def status_port(self) -> int:
        return self.data.get("status_port", DEFAULT_STATUS_PORT)

    @property
    def id(self) -> str:
        return f"{self.region}-{self.data['city']}-{self.data['number']}"

    @property
    def city_display(self) -> str:
        return f"{self.region_name} [{self.city_name}]"

    @property
    def display_name(self) -> str:
        return f"{self.city_display} #{self.data['number']}"


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def load_json(path: Path, report: Report):
    def no_duplicates(pairs):
        keys = [k for k, _ in pairs]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise ValueError(f"duplicate key(s): {', '.join(dupes)}")
        return dict(pairs)

    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        report.error(rel(path), "file starts with a UTF-8 BOM; save it as UTF-8 without BOM")
        return None
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        report.error(rel(path), f"not valid JSON: {exc}")
        return None


def schema_validator(name: str) -> Draft202012Validator:
    schema = json.loads((ROOT / "schema" / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def schema_errors(validator: Draft202012Validator, data, path: str, report: Report) -> bool:
    found = False
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "(root)"
        report.error(path, f"schema: {where}: {err.message}")
        found = True
    return found


def slug(name: str) -> str:
    return re.sub(r"[ -]+", "-", name.replace("'", "").lower())


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


# ---------------------------------------------------------------- locations


def load_locations(report: Report):
    path = ROOT / LOCATIONS_FILE
    data = load_json(path, report)
    if data is None or schema_errors(schema_validator("locations.schema.json"), data, LOCATIONS_FILE, report):
        return None

    regions = {}
    for r in data["regions"]:
        if r["id"] in regions:
            report.error(LOCATIONS_FILE, f"region id '{r['id']}' is listed twice")
        regions[r["id"]] = r
    if len({r["name"] for r in data["regions"]}) != len(data["regions"]):
        report.error(LOCATIONS_FILE, "region names must be unique")

    cities = {}
    names = set()
    order = list(regions)
    previous = None
    for c in data["cities"]:
        where = f"city '{c['id']}'"
        if c["id"] in cities:
            report.error(LOCATIONS_FILE, f"{where} is listed twice")
        if c["name"].lower() in names:
            report.error(LOCATIONS_FILE, f"{where}: the name '{c['name']}' is already used by another city")
        if c["id"] != slug(c["name"]):
            report.error(LOCATIONS_FILE, f"{where}: id must be '{slug(c['name'])}', derived from the name '{c['name']}'")
        if c["region"] not in regions:
            report.error(LOCATIONS_FILE, f"{where}: unknown region '{c['region']}' (allowed: {', '.join(regions)})")
        else:
            key = (order.index(c["region"]), c["id"])
            if previous is not None and key < previous:
                report.error(LOCATIONS_FILE, f"{where}: cities must be grouped by region in the order of 'regions' and sorted by id inside each region")
            previous = key
        cities[c["id"]] = c
        names.add(c["name"].lower())
    return regions, cities


# ---------------------------------------------------------------- servers


def check_host(server: Server, report: Report) -> None:
    host = server.data["host"]
    if ":" in host or re.fullmatch(r"[0-9.]+", host):
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            report.error(server.path, f"host '{host}' is not a valid IP address")
            return
        if str(ip) != host:
            report.error(server.path, f"host: write the address as '{ip}'")
        if not ip.is_global:
            report.error(server.path, f"host '{host}' is not a public address (private, loopback, CGNAT, documentation or reserved range)")
        return

    labels = host.split(".")
    if len(labels) < 2 or len(host) > 253 or not all(HOSTNAME_LABEL.match(label) for label in labels):
        report.error(server.path, f"host '{host}' is not a valid DNS name (lowercase letters, digits, '-', at least two labels)")
        return
    if not TLD.match(labels[-1]):
        report.error(server.path, f"host '{host}' has an invalid top-level domain")
    for suffix in RESERVED_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            report.error(server.path, f"host '{host}' uses the reserved name '{suffix}', which is not reachable on the internet")
            break


def resolve(host: str, port: int) -> set[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    return {info[4][0] for info in infos}


def check_dns(servers: list[Server], report: Report) -> None:
    def note(server: Server, message: str) -> None:
        # A broken DNS name of an untouched entry must not block other operators' pull requests.
        if server.status == "unchanged":
            report.warn(server.path, message)
        else:
            report.error(server.path, message)

    names = [s for s in servers if not is_ip(s.data["host"])]
    resolved: dict[str, set[str]] = {}
    pool = ThreadPoolExecutor(max_workers=16)
    futures = {s.path: pool.submit(resolve, s.data["host"], s.data["port"]) for s in names}
    for s in names:
        try:
            resolved[s.path] = futures[s.path].result(timeout=DNS_TIMEOUT)
        except FutureTimeout:
            note(s, f"host '{s.data['host']}' did not resolve within {DNS_TIMEOUT:.0f} s")
        except OSError as exc:
            note(s, f"host '{s.data['host']}' does not resolve: {exc}")
    pool.shutdown(wait=False, cancel_futures=True)

    endpoints: dict[tuple[str, int], Server] = {}
    for s in servers:
        addresses = {s.data["host"]} if is_ip(s.data["host"]) else resolved.get(s.path, set())
        for address in sorted(addresses):
            ip = ipaddress.ip_address(address.split("%")[0])
            if not ip.is_global:
                note(s, f"host '{s.data['host']}' resolves to the non-public address {ip}")
                continue
            key = (str(ip), s.data["port"])
            other = endpoints.get(key)
            if other is not None and other is not s:
                report.error(s.path, f"{ip}:{s.data['port']} is the same server as {other.path}")
            endpoints.setdefault(key, s)


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def check_numbers(servers: list[Server], report: Report) -> None:
    by_city: dict[str, list[Server]] = {}
    for s in servers:
        by_city.setdefault(s.data["city"], []).append(s)
    for entries in by_city.values():
        kept = {s.data["number"] for s in entries if s.status != "added"}
        added = sorted((s for s in entries if s.status == "added"), key=lambda s: s.data["number"])
        free = []
        n = 1
        while len(free) < len(added):
            if n not in kept:
                free.append(n)
            n += 1
        for s, expected in zip(added, free):
            if s.data["number"] != expected:
                report.error(s.path, f"a new server in {s.city_name} must take #{expected}, the smallest free number "
                                     f"(rename the file to servers/{s.region}/{s.data['city']}-{expected}.json and set \"number\": {expected})")


def load_servers(locations, report: Report, base: str | None) -> list[Server]:
    regions, cities = locations
    validator = schema_validator("server.schema.json")

    base_paths: set[str] | None = None
    changed: set[str] = set()
    if base:
        base_paths = set(git("ls-tree", "-r", "--name-only", base, "--", SERVERS_DIR).split())
        changed = set(git("diff", "--name-only", base, "--", SERVERS_DIR).split())

    servers = []
    for file in sorted((ROOT / SERVERS_DIR).rglob("*")):
        if file.is_dir():
            continue
        path = rel(file)
        if path == PLACEHOLDER:
            continue
        match = SERVER_FILE.match(path)
        if not match:
            report.error(path, "only server entries named servers/<region>/<city>-<number>.json belong in servers/")
            continue
        data = load_json(file, report)
        if data is None or schema_errors(validator, data, path, report):
            continue

        server = Server(path, data)
        if base_paths is not None:
            server.status = "added" if path not in base_paths else ("changed" if path in changed else "unchanged")

        city = cities.get(data["city"])
        if city is None:
            report.error(path, f"unknown city '{data['city']}'; add it to {LOCATIONS_FILE} in the same pull request")
            continue
        server.region = city["region"]
        server.region_name = regions[city["region"]]["name"]
        server.city_name = city["name"]
        expected = f"servers/{server.region}/{data['city']}-{data['number']}.json"
        if path != expected:
            report.error(path, f"file name does not match the content: expected {expected}")
            continue
        for ch in data["operator"]["name"]:
            if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs"):
                report.error(path, f"operator.name contains an invisible or control character U+{ord(ch):04X}")
                break
        # The game renders rich-text tags in labels; a name must not be able to restyle the menu.
        if "<" in data["operator"]["name"] or ">" in data["operator"]["name"]:
            report.error(path, "operator.name must not contain '<' or '>'")
        if server.status_port == LUXON_WEB_PORT:
            report.error(path, f"status_port {LUXON_WEB_PORT} is LuxonServer's own web API, which must never be public; "
                               f"use the haulnet-server status endpoint (default {DEFAULT_STATUS_PORT})")
        check_host(server, report)
        servers.append(server)

    seen: dict[tuple[str, int], Server] = {}
    for s in servers:
        key = (s.data["host"], s.data["port"])
        if key in seen:
            report.error(s.path, f"{key[0]} port {key[1]} is already listed in {seen[key].path}")
        seen.setdefault(key, s)

    check_numbers(servers, report)
    return servers


# ---------------------------------------------------------------- status endpoint


class StatusError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a status endpoint answers directly; a redirect surfaces as HTTP 3xx


def status_url(host: str, port: int) -> str:
    return f"http://{'[' + host + ']' if ':' in host else host}:{port}/status"


def fetch_status(host: str, port: int, timeout: float = STATUS_TIMEOUT) -> dict:
    """GET /status of a haulnet-server; raises StatusError with a reason an operator can act on."""
    request = urllib.request.Request(status_url(host, port),
                                     headers={"User-Agent": "haulnet-servers-ci", "Accept": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(STATUS_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            raise StatusError("answers HTTP 503: LuxonServer is not running or not answering") from None
        raise StatusError(f"answers HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise StatusError(f"is not reachable ({reason}); open TCP port {port} or set status_port") from None
    if len(body) > STATUS_MAX_BYTES:
        raise StatusError("answer is larger than 64 KiB")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise StatusError("answer is not JSON") from None


def fetch_with_retry(host: str, port: int) -> dict:
    for attempt in range(STATUS_ATTEMPTS):
        try:
            return fetch_status(host, port)
        except StatusError as exc:
            permanent = "HTTP 4" in str(exc) or "HTTP 3" in str(exc) or "JSON" in str(exc) or "64 KiB" in str(exc)
            if permanent or attempt + 1 == STATUS_ATTEMPTS:
                raise
            time.sleep(2)
    raise AssertionError("unreachable")


def check_status(servers: list[Server], supported: list[str], report: Report) -> None:
    failed = {path for path, _ in report.errors}
    targets = [s for s in servers if s.status != "unchanged" and s.path not in failed]
    if not targets:
        return
    validator = schema_validator("status.schema.json")
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {s.path: pool.submit(fetch_with_retry, s.data["host"], s.status_port) for s in targets}
        for s in targets:
            url = status_url(s.data["host"], s.status_port)
            try:
                data = futures[s.path].result()
            except StatusError as exc:
                report.error(s.path, f"status endpoint {url} {exc}")
                continue
            problems = Report()
            if schema_errors(validator, data, s.path, problems):
                details = "; ".join(m.removeprefix("schema: ") for _, m in problems.errors)
                report.error(s.path, f"status endpoint {url} does not answer like haulnet-server: {details}")
                continue
            s.probe = data
            if data["luxon"] not in supported:
                report.error(s.path, f"the server runs LuxonServer {data['luxon']}, which does not work with the current game "
                                     f"(supported: {', '.join(supported)}); update haulnet-server")
            if not data["accepting"]:
                report.warn(s.path, "the server reports that it does not accept new games right now")


def load_compatibility(report: Report) -> list[str] | None:
    data = load_json(ROOT / COMPATIBILITY_FILE, report)
    if data is None or schema_errors(schema_validator("compatibility.schema.json"), data, COMPATIBILITY_FILE, report):
        return None
    return data["luxon"]


def validate(base: str | None, dns: bool, probe: bool = False):
    report = Report()
    locations = load_locations(report)
    supported = load_compatibility(report)
    servers: list[Server] = []
    if locations is not None:
        servers = load_servers(locations, report, base)
        if dns and not any(path.startswith(SERVERS_DIR) for path, _ in report.errors):
            check_dns(servers, report)
        if probe and supported is not None:
            check_status(servers, supported, report)
    return report, locations, supported, servers


def summary(servers: list[Server], report: Report) -> str:
    lines = [f"**{len(servers)}** server(s) in the registry; {len(report.errors)} error(s), {len(report.warnings)} warning(s)."]
    touched = [s for s in servers if s.status != "unchanged"]
    if touched:
        lines += ["", "| Change | Name | Address | Operator | Status endpoint |", "|---|---|---|---|---|"]
        for s in touched:
            op = s.data["operator"]
            name = op["name"].replace("|", "\\|")
            if s.probe:
                p = s.probe
                state = (f"haulnet-server {p['haulnet_server']}, LuxonServer {p['luxon']}, "
                         f"{p['rooms']} room(s), {p['players']} player(s)").replace("|", "\\|")
            else:
                state = "not checked"
            lines.append(f"| {s.status} | {s.display_name} | `{s.data['host']}:{s.data['port']}` | {name} (@{op['github']}) | {state} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- build


def build_list(locations, supported: list[str], servers: list[Server], commit: str, updated_at: str) -> dict:
    regions, cities = locations
    region_order = list(regions)
    servers = sorted(servers, key=lambda s: (region_order.index(s.region), s.city_name, s.data["number"]))
    used = []
    for s in servers:
        if s.data["city"] not in used:
            used.append(s.data["city"])

    def city_entry(cid: str) -> dict:
        c = cities[cid]
        return {**{k: c[k] for k in ("id", "name", "country", "region")},
                "display_name": f"{regions[c['region']]['name']} [{c['name']}]"}

    def server_entry(s: Server) -> dict:
        op = {k: s.data["operator"][k] for k in ("name", "github", "contact") if k in s.data["operator"]}
        return {"id": s.id, "display_name": s.display_name, "region": s.region, "city": s.data["city"],
                "number": s.data["number"], "host": s.data["host"], "port": s.data["port"],
                "status_port": s.status_port, "operator": op}

    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": updated_at,
        "source_commit": commit,
        "compatibility": {"luxon": list(supported)},
        "regions": [{"id": r["id"], "name": r["name"]} for r in regions.values()],
        "cities": [city_entry(c) for c in used],
        "servers": [server_entry(s) for s in servers],
    }


def render_index(listing: dict) -> str:
    dash = '<td data-col="state">—</td><td data-col="load">—</td><td data-col="version">—</td><td data-col="uptime">—</td>'
    rows = "\n".join(
        f'<tr data-id="{html.escape(s["id"])}"><td>{html.escape(s["display_name"])}</td>{dash}'
        f'<td><code>{html.escape(s["host"])}:{s["port"]}</code></td>'
        f'<td>{html.escape(s["operator"]["name"])}</td></tr>'
        for s in listing["servers"]
    ) or '<tr><td colspan="7">No servers yet.</td></tr>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Haulnet servers</title>
<link rel="icon" href="favicon.ico" sizes="32x32">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<style>
:root {{ --ok: #1a7f37; --warn: #9a6700; --bad: #cf222e; --muted: #59636e; }}
@media (prefers-color-scheme: dark) {{ :root {{ --ok: #3fb950; --warn: #d29922; --bad: #f85149; --muted: #9198a1; }} }}
body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 64rem; margin: 2rem auto; padding: 0 1rem; }}
.wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ text-align: left; padding: .35rem .5rem; border-bottom: 1px solid #8884; }}
th, td[data-col], td code {{ white-space: nowrap; }}
#status-note {{ color: var(--muted); font-size: 13px; }}
.ok {{ color: var(--ok); }} .warn {{ color: var(--warn); }} .bad {{ color: var(--bad); }}
</style>
</head>
<body>
<h1>Haulnet community servers</h1>
<p>Machine-readable list: <a href="v1/servers.json">v1/servers.json</a> (schema version {listing['schema_version']}).
Updated {html.escape(listing['updated_at'])}. Source:
<a href="https://github.com/haulnet/haulnet-servers">haulnet/haulnet-servers</a>.</p>
<div class="wrap">
<table data-status-url="{html.escape(MONITOR_STATUS_URL)}">
<thead><tr><th>Server</th><th>Status</th><th>Load</th><th>Version</th><th>Uptime (30 days)</th><th>Address</th><th>Operator</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
<p id="status-note">Live status needs JavaScript.</p>
<script src="status.js" defer></script>
</body>
</html>
"""


def build(out: Path, commit: str | None, updated_at: str | None) -> int:
    report, locations, supported, servers = validate(base=None, dns=False)
    report.emit()
    if report.errors:
        print(f"Registry has {len(report.errors)} error(s); nothing was built.", file=sys.stderr)
        return 1

    commit = commit or git("rev-parse", "HEAD").strip()
    if not updated_at:
        stamp = int(git("log", "-1", "--format=%ct").strip())
        updated_at = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    listing = build_list(locations, supported, servers, commit, updated_at)

    check = Report()
    if schema_errors(schema_validator(f"servers-v{SCHEMA_VERSION}.schema.json"), listing, "v1/servers.json", check):
        check.emit()
        return 1

    (out / "v1").mkdir(parents=True, exist_ok=True)
    (out / "schema").mkdir(parents=True, exist_ok=True)
    (out / "v1" / "servers.json").write_text(json.dumps(listing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    for schema in sorted((ROOT / "schema").glob("*.json")):
        (out / "schema" / schema.name).write_bytes(schema.read_bytes())
    # Static files of the web page (favicons), copied as they are.
    for asset in sorted((ROOT / "pages").iterdir()):
        (out / asset.name).write_bytes(asset.read_bytes())
    (out / "index.html").write_text(render_index(listing), encoding="utf-8", newline="\n")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Built {out / 'v1' / 'servers.json'}: {len(listing['servers'])} server(s) in {len(listing['cities'])} city(ies).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate", help="check the registry")
    v.add_argument("--base", help="git ref of the pull request base; enables the rules for added entries")
    v.add_argument("--no-dns", action="store_true", help="skip DNS resolution and duplicate-IP checks")
    v.add_argument("--probe", action="store_true", help="ask the status endpoint of added or changed entries")
    b = sub.add_parser("build", help="build the published list")
    b.add_argument("--out", required=True, type=Path)
    b.add_argument("--commit", help="override the source commit (default: HEAD)")
    b.add_argument("--updated-at", help="override the timestamp (default: commit time of HEAD)")
    args = parser.parse_args()

    if args.command == "build":
        return build(args.out, args.commit, args.updated_at)

    report, _, _, servers = validate(args.base, dns=not args.no_dns, probe=args.probe)
    report.emit()
    text = summary(servers, report)
    print(text, end="")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write("## Registry check\n\n" + text)
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
