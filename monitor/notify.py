"""Operator notifications of the Haulnet registry monitor: GitHub issues in the registry repository.

One issue per server and problem (`down`, `incompatible`, `outdated`), found again by a hidden marker in its body.
Without a token the notifier runs dry: it logs what it would do, once per change. Standard library only.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("haulnet-monitor")

DOWN_AFTER = 3600  # offline this long → issue
INCOMPATIBLE_AFTER = 1800
OUTDATED_AFTER = 14 * 86400  # behind the latest release by a patch version
OUTDATED_MINOR_AFTER = 2 * 86400  # behind by a minor or major version
REMOVAL_NOTE_AFTER = 7 * 86400  # README: unreachable for a week may be removed
RELEASE_REFRESH = 6 * 3600
BACKOFF = 600  # after a GitHub API error, wait before the next write
STATUS_PAGE = "https://servers.haulnet.cc/"
RULES = "https://github.com/haulnet/haulnet-servers#rules-for-operators"
UPDATING = "https://haulnet.cc/docs/server/#updating"
GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
MARKER = re.compile(r"<!-- haulnet-monitor:([a-z0-9-]+):([a-z]+) -->")
VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")
KINDS = ("down", "incompatible", "outdated")

SCHEMA = """
CREATE TABLE IF NOT EXISTS conditions (server_id TEXT NOT NULL, kind TEXT NOT NULL, since INTEGER NOT NULL,
    PRIMARY KEY (server_id, kind));
CREATE TABLE IF NOT EXISTS issues (server_id TEXT NOT NULL, kind TEXT NOT NULL, number INTEGER NOT NULL,
    opened_at INTEGER NOT NULL, removal_noted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (server_id, kind));
"""


@dataclass
class ServerView:
    """What the notifier needs to know about one listed server after a check round."""
    id: str
    display_name: str
    host: str
    status_port: int
    operator_github: str | None
    state: str  # pending | online | offline
    since: int | None
    last_error: str | None
    answer: dict | None  # last successful /status answer (typed fields)
    compatible: bool | None


def version(text: str | None) -> tuple[int, int, int] | None:
    match = VERSION.match(text or "")
    return tuple(int(x) for x in match.groups()) if match else None  # type: ignore[return-value]


def utc(ts: float) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d %H:%M")


class GitHubError(Exception):
    pass


class Notifier:
    def __init__(self, db_path: str, repo: str, token: str | None, *, server_repo: str = "haulnet/haulnet-server",
                 api: str = "https://api.github.com", location: str | None = None):
        self.repo, self.token, self.server_repo, self.api = repo, token, server_repo, api.rstrip("/")
        self.location = location
        self.db = sqlite3.connect(db_path, timeout=30)
        self.db.executescript(SCHEMA)
        if token:
            # Dry-run records (number 0) were never written to GitHub: forget them so real issues get opened.
            self.db.execute("DELETE FROM issues WHERE number = 0")
            self.db.commit()
        self.latest: tuple[tuple[int, int, int], str, int] | None = None  # (version, text, published ts)
        self.latest_checked = 0.0
        self.blocked_until = 0.0
        self.synced = not token

    @property
    def dry(self) -> bool:
        return not self.token

    # --- GitHub ---

    def _request(self, method: str, path: str, body: dict | None = None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "haulnet-monitor"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.api + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            raise GitHubError(f"{method} {path}: HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise GitHubError(f"{method} {path}: {exc}") from None
        return json.loads(raw.decode("utf-8")) if raw else None

    def refresh_release(self, now: float) -> None:
        if now - self.latest_checked < RELEASE_REFRESH:
            return
        self.latest_checked = now
        try:
            data = self._request("GET", f"/repos/{self.server_repo}/releases/latest")
        except GitHubError as exc:
            log.warning("latest haulnet-server release: %s", exc)
            self.latest_checked = now - RELEASE_REFRESH + BACKOFF  # try again soon, not in six hours
            return
        parsed = version(data.get("tag_name") if isinstance(data, dict) else None)
        published = data.get("published_at") if isinstance(data, dict) else None
        if parsed is None or not isinstance(published, str):
            return
        ts = int(datetime.strptime(published[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
        self.latest = (parsed, ".".join(map(str, parsed)), ts)

    def sync(self) -> None:
        """Adopts open issues with our marker (after a database loss), so none is opened twice."""
        if self.synced or self.dry:
            return
        page = 1
        while True:
            items = self._request("GET", f"/repos/{self.repo}/issues?state=open&per_page=100&page={page}") or []
            for item in items:
                match = MARKER.search(item.get("body") or "")
                if match and "pull_request" not in item:
                    opened = datetime.strptime(item["created_at"][:19], "%Y-%m-%dT%H:%M:%S")
                    self.db.execute("INSERT OR IGNORE INTO issues (server_id, kind, number, opened_at) VALUES (?,?,?,?)",
                                    (match.group(1), match.group(2), item["number"],
                                     int(opened.replace(tzinfo=timezone.utc).timestamp())))
            if len(items) < 100:
                break
            page += 1
        self.db.commit()
        self.synced = True

    # --- decisions ---

    def _condition(self, sid: str, kind: str, active: bool, now: int) -> int | None:
        """Remembers since when a condition holds; returns that time, or None when it does not hold."""
        row = self.db.execute("SELECT since FROM conditions WHERE server_id = ? AND kind = ?", (sid, kind)).fetchone()
        if not active:
            if row:
                self.db.execute("DELETE FROM conditions WHERE server_id = ? AND kind = ?", (sid, kind))
            return None
        if row:
            return row[0]
        self.db.execute("INSERT INTO conditions (server_id, kind, since) VALUES (?,?,?)", (sid, kind, now))
        return now

    def plan(self, servers: list[ServerView], compatibility: list[str], now: int) -> list[tuple]:
        """Returns actions: ("open", view, kind, title, body), ("close", sid, kind, number, comment),
        ("note", sid, kind, number, comment)."""
        actions: list[tuple] = []
        listed = {s.id for s in servers}
        issues = {(sid, kind): (number, opened, noted) for sid, kind, number, opened, noted in
                  self.db.execute("SELECT server_id, kind, number, opened_at, removal_noted FROM issues")}
        for s in servers:
            running = version((s.answer or {}).get("haulnet_server"))
            outdated_since = None
            lag_threshold = OUTDATED_AFTER
            if self.latest and running and s.state == "online":
                if running < self.latest[0]:
                    if running[:2] < self.latest[0][:2]:
                        lag_threshold = OUTDATED_MINOR_AFTER
                    outdated_since = self._condition(s.id, "outdated", True, now)
                else:
                    self._condition(s.id, "outdated", False, now)
            # While a server is offline its last known version stays as it was.
            incompatible = s.state == "online" and s.compatible is False
            incompatible_since = self._condition(s.id, "incompatible", incompatible, now)
            active = {
                "down": s.state == "offline" and s.since is not None and now - s.since >= DOWN_AFTER,
                "incompatible": incompatible_since is not None and now - incompatible_since >= INCOMPATIBLE_AFTER,
                "outdated": outdated_since is not None and now - outdated_since >= lag_threshold,
            }
            resolved = {
                "down": s.state == "online",
                "incompatible": s.state == "online" and s.compatible is not False,
                "outdated": s.state == "online" and running is not None and self.latest is not None
                            and running >= self.latest[0],
            }
            for kind in KINDS:
                issue = issues.get((s.id, kind))
                if issue is None and active[kind]:
                    actions.append(("open", s, kind) + self._text(s, kind, compatibility))
                elif issue is not None and resolved[kind]:
                    actions.append(("close", s.id, kind, issue[0], self._closing(s, kind, now)))
                elif issue is not None and kind == "down" and not issue[2] and s.since is not None \
                        and now - s.since >= REMOVAL_NOTE_AFTER:
                    actions.append(("note", s.id, kind, issue[0],
                                    f"Unreachable for 7 days. Under the [rules for operators]({RULES}), maintainers may "
                                    "now remove this server from the list; it can come back with a new pull request once "
                                    "it is up again."))
        for (sid, kind), (number, _, _) in issues.items():
            if sid not in listed:
                actions.append(("close", sid, kind, number, "The server is no longer in the list. Closing."))
        self.db.execute("DELETE FROM conditions WHERE server_id NOT IN (%s)" % ",".join("?" * len(listed)) if listed
                        else "DELETE FROM conditions", tuple(listed))
        self.db.commit()
        return actions

    def _text(self, s: ServerView, kind: str, compatibility: list[str]) -> tuple[str, str]:
        mention = f"@{s.operator_github}: " if s.operator_github and GITHUB_LOGIN.match(s.operator_github) else ""
        where = f" in {self.location}" if self.location else ""
        url = f"http://{'[' + s.host + ']' if ':' in s.host else s.host}:{s.status_port}/status"
        answer = s.answer or {}
        marker = f"<!-- haulnet-monitor:{s.id}:{kind} -->"
        if kind == "down":
            title = f"{s.display_name} ({s.id}) is unreachable"
            text = (f"{mention}The Haulnet registry monitor{where} has had no answer from `{url}` since "
                    f"{utc(s.since or 0)} UTC (last error: `{s.last_error or 'unknown'}`).\n\n"
                    "While the server is unreachable, Haulnet does not offer it to players. Please check that "
                    "haulnet-server is running and that UDP 27000–27002 and TCP "
                    f"{s.status_port} are open.\n\nThis issue closes by itself when the server answers again. A server "
                    f"that stays unreachable for a week may be removed from the list ([rules for operators]({RULES})).")
        elif kind == "incompatible":
            title = f"{s.display_name} ({s.id}) runs an unsupported LuxonServer"
            text = (f"{mention}{s.display_name} runs LuxonServer `{answer.get('luxon')}`, which does not work with "
                    f"the current game (supported: {', '.join(f'`{v}`' for v in compatibility) or 'none listed'}). "
                    f"Haulnet hides it from players until it is updated: [how to update]({UPDATING}).\n\n"
                    "This issue closes by itself when the server reports a supported version.")
        else:
            latest = self.latest[1] if self.latest else "?"
            title = f"{s.display_name} ({s.id}): haulnet-server {answer.get('haulnet_server')} is out of date"
            text = (f"{mention}{s.display_name} runs haulnet-server `{answer.get('haulnet_server')}`; the latest "
                    f"release is `{latest}`. Please update (`sudo ./haulnet-update`), or turn on automatic updates "
                    f"(`sudo ./haulnet-update enable`): [how to update]({UPDATING}).\n\n"
                    "This issue closes by itself when the server runs the latest release.")
        footer = f"\n\nLive status: {STATUS_PAGE}\n\n_Opened automatically by the Haulnet registry monitor._"
        return title, f"{marker}\n{text}{footer}"

    @staticmethod
    def _closing(s: ServerView, kind: str, now: int) -> str:
        if kind == "down":
            return f"The server answers again (checked {utc(now)} UTC). Closing."
        if kind == "incompatible":
            return f"The server reports a supported LuxonServer version again (checked {utc(now)} UTC). Closing."
        return f"The server runs the latest haulnet-server release (checked {utc(now)} UTC). Closing."

    # --- actions ---

    def evaluate(self, servers: list[ServerView], compatibility: list[str], now: float | None = None) -> None:
        now_i = int(now if now is not None else time.time())
        if now_i < self.blocked_until:
            return
        try:
            self.refresh_release(now_i)
            self.sync()
            for action in self.plan(servers, compatibility, now_i):
                self._apply(action, now_i)
        except GitHubError as exc:
            log.warning("issues: %s; retrying in %d min", exc, BACKOFF // 60)
            self.blocked_until = now_i + BACKOFF

    def _apply(self, action: tuple, now: int) -> None:
        verb = action[0]
        if verb == "open":
            _, s, kind, title, body = action
            number = 0
            if self.dry:
                log.info("issues (dry run): would open %r", title)
            else:
                created = self._request("POST", f"/repos/{self.repo}/issues",
                                        {"title": title, "body": body, "labels": ["server-problem"]})
                number = int(created["number"])
                log.info("issues: opened #%d %r", number, title)
            self.db.execute("INSERT OR REPLACE INTO issues (server_id, kind, number, opened_at) VALUES (?,?,?,?)",
                            (s.id, kind, number, now))
        elif verb == "close":
            _, sid, kind, number, comment = action
            if self.dry or number == 0:
                log.info("issues (dry run): would close %s/%s: %s", sid, kind, comment)
            else:
                self._request("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": comment})
                self._request("PATCH", f"/repos/{self.repo}/issues/{number}",
                              {"state": "closed", "state_reason": "completed"})
                log.info("issues: closed #%d (%s/%s)", number, sid, kind)
            self.db.execute("DELETE FROM issues WHERE server_id = ? AND kind = ?", (sid, kind))
        elif verb == "note":
            _, sid, kind, number, comment = action
            if self.dry or number == 0:
                log.info("issues (dry run): would comment on %s/%s: unreachable for 7 days", sid, kind)
            else:
                self._request("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": comment})
                log.info("issues: removal note on #%d (%s)", number, sid)
            self.db.execute("UPDATE issues SET removal_noted = 1 WHERE server_id = ? AND kind = ?", (sid, kind))
        self.db.commit()

    def close(self) -> None:
        self.db.close()
