"""Single-owner Slack adapter. Message content never grants permissions."""

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    slack_team_id: str | None = Field(pattern=r"^T[A-Z0-9]+$")
    slack_owner_id: str | None = Field(pattern=r"^U[A-Z0-9]+$")
    slack_rooms: list[str] = Field(default_factory=list)
    slack_send_enabled: bool = False
    owner_alert_enabled: bool = False


STAMP = re.compile(r"^[0-9]{1,16}\.[0-9]{1,6}$")
SEOUL = ZoneInfo("Asia/Seoul")
RETENTION = 7 * 86400


class SlackService:
    def __init__(self, state: Path, config: Config, credentials: Path):
        if any(not re.fullmatch(r"C[A-Z0-9]+", room) for room in config.slack_rooms):
            raise ValueError("Only public channel IDs are supported")
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = state / "slack.sqlite3"
        self.config, self.credentials = config, credentials
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, room TEXT NOT NULL, sender TEXT NOT NULL,
                    ts TEXT NOT NULL, thread_ts TEXT NOT NULL, text TEXT NOT NULL,
                    reason TEXT NOT NULL, received REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', UNIQUE(room, ts));
                CREATE TABLE IF NOT EXISTS outgoing (
                    event_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
                    channel TEXT, ts TEXT, PRIMARY KEY(event_id, kind));
            """)
        self.db.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def secret(self, name):
        path = self.credentials / name
        return path.read_text().strip() if path.is_file() else ""

    def allowed(self, room):
        if room not in self.config.slack_rooms:
            raise ToolError("Channel is not owner-allowlisted; a mention does not grant read/reply access")

    def prune(self, db):
        db.execute("DELETE FROM outgoing WHERE event_id IN (SELECT id FROM events WHERE received < ?)",
                   (time.time() - RETENTION,))
        db.execute("DELETE FROM events WHERE received < ?", (time.time() - RETENTION,))

    def ingest(self, payload):
        if not self.config.slack_team_id or not self.config.slack_owner_id:
            return False
        if payload.get("team_id") != self.config.slack_team_id:
            return False
        event = payload.get("event")
        if not isinstance(event, dict) or event.get("type") != "message":
            return False
        if event.get("bot_id") or event.get("bot_profile") or event.get("subtype"):
            return False
        sender, room, text = event.get("user"), event.get("channel"), event.get("text")
        identity, ts = payload.get("event_id"), event.get("ts")
        thread_ts = event.get("thread_ts", ts)
        if not all(isinstance(v, str) and v for v in (sender, room, text, identity, ts, thread_ts)):
            return False
        if (sender == self.config.slack_owner_id or not re.fullmatch(r"U[A-Z0-9]+", sender)
                or not re.fullmatch(r"C[A-Z0-9]+", room) or len(identity) > 200
                or not STAMP.fullmatch(ts) or not STAMP.fullmatch(thread_ts)):
            return False
        if not time.time() - RETENTION <= float(ts) <= time.time() + 300:
            return False
        mentioned = f"<@{self.config.slack_owner_id}>" in text
        if not mentioned and room not in self.config.slack_rooms:
            return False
        with self.connect() as db:
            self.prune(db)
            changed = db.execute("""INSERT OR IGNORE INTO events
                (id,room,sender,ts,thread_ts,text,reason,received) VALUES (?,?,?,?,?,?,?,?)""",
                (identity, room, sender, ts, thread_ts, text[:12000],
                 "owner_mention" if mentioned else "selected_room", time.time())).rowcount
        return bool(changed)

    def pending(self, limit):
        with self.connect() as db:
            self.prune(db)
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM events WHERE status='pending' ORDER BY received,id LIMIT ?", (limit,))]
        for row in rows:
            row["context_reply_allowed"] = row["room"] in self.config.slack_rooms
        return {"events": rows, "execution_automatic": False,
                "notification_automatic": False, "retention_days": 7}

    def event(self, identity):
        with self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE id=? AND received>=?",
                             (identity, time.time() - RETENTION)).fetchone()
        if not row:
            raise ToolError("Event not found or expired")
        return dict(row)

    def slack(self, method, payload):
        if not self.config.slack_team_id or not self.config.slack_owner_id:
            raise ToolError("Slack is unconfigured; provision both team and human owner IDs")
        token = self.secret("slack-token")
        if not token:
            raise ToolError("Slack is not connected; OAuth token has not been provisioned")
        req = Request("https://slack.com/api/" + method, data=json.dumps(payload).encode(),
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json; charset=utf-8"})
        try:
            with urlopen(req, timeout=15) as response:
                result = json.loads(response.read(2_000_000))
        except HTTPError as error:
            raise ToolError(f"Slack HTTP {error.code}; never blindly retry a send") from None
        except (OSError, ValueError):
            raise ToolError("Slack outcome unknown; inspect Slack before any send retry") from None
        if not isinstance(result, dict) or not result.get("ok"):
            raise ToolError("Slack rejected request; check app scopes and channel membership")
        return result

    def read(self, room, oldest, limit):
        self.allowed(room)
        result = self.slack("conversations.history", {"channel": room, "oldest": oldest, "limit": limit})
        return {key: result.get(key) for key in ("messages", "has_more", "response_metadata")}

    def context(self, identity):
        event = self.event(identity)
        self.allowed(event["room"])
        result = self.slack("conversations.replies", {"channel": event["room"],
                            "ts": event["thread_ts"], "limit": 50})
        metadata = result.get("response_metadata") or {}
        return {"event": event, "messages": result.get("messages", []),
                "has_more": result.get("has_more"), "response_metadata": metadata,
                "thread_complete": result.get("has_more") is False and not metadata.get("next_cursor"),
                "limitation": "One page only; unsupported tokens or missing scopes fail explicitly; ask for clarification when insufficient"}

    def send_once(self, event, kind, payload):
        if not self.secret("slack-token"):
            raise ToolError("Slack is not connected; no send attempted")
        with self.connect() as db:
            changed = db.execute("INSERT OR IGNORE INTO outgoing(event_id,kind,status) VALUES (?,?,'unknown')",
                                 (event["id"], kind)).rowcount
            if not changed:
                row = db.execute("SELECT status,channel,ts FROM outgoing WHERE event_id=? AND kind=?",
                                 (event["id"], kind)).fetchone()
                return {**dict(row), "sent": row["status"] == "sent", "attempted_now": False}
        # Reserve BEFORE network I/O. A crash, error, or lost response never authorizes a retry.
        result = self.slack("chat.postMessage", {**payload, "unfurl_links": False, "unfurl_media": False})
        if not result.get("channel") or not result.get("ts"):
            raise ToolError("Slack response incomplete; send outcome remains unknown")
        with self.connect() as db:
            db.execute("UPDATE outgoing SET status='sent',channel=?,ts=? WHERE event_id=? AND kind=?",
                       (result["channel"], result["ts"], event["id"], kind))
        return {"status": "sent", "sent": True, "channel": result["channel"], "ts": result["ts"], "attempted_now": True}

    def reply(self, identity, text):
        event = self.event(identity)
        self.allowed(event["room"])
        if not self.config.slack_send_enabled:
            raise ToolError("Replies are disabled by owner")
        return self.send_once(event, "reply", {"channel": event["room"], "thread_ts": event["thread_ts"], "text": text})

    def notify(self, identity, summary, reason, quote, impact, deadline):
        event = self.event(identity)
        if not self.config.owner_alert_enabled:
            raise ToolError("Owner notifications are disabled; no notification sent")
        if quote not in event["text"]:
            raise ToolError("Evidence must be an exact quote from the received event")
        now = datetime.now(SEOUL)
        work = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if work <= now:
            work += timedelta(days=1)
        while work.weekday() >= 5:
            work += timedelta(days=1)
        due = None
        if deadline:
            try:
                due = datetime.fromisoformat(deadline)
                if due.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise ToolError("Deadline must be ISO 8601 with a timezone") from None
        if impact == "none" and not (due and now < due < work):
            raise ToolError("Urgency requires current outage/security impact or verified deadline before next weekday 09:00 Seoul")
        text = (f"After-hours urgent review\n{summary}\nReason: {reason}\nEvidence: {quote}\n"
                f"Impact: {impact}; deadline: {deadline or 'none'}\n"
                f"Source: {event['room']} / {event['ts']} (event {identity})")
        return self.send_once(event, "owner_alert", {"channel": self.config.slack_owner_id,
                                                   "text": text, "mrkdwn": False})
