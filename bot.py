#!/usr/bin/env python3
"""
Telegram group watchdog (userbot) v2.

Runs on YOUR Telegram account (MTProto - Bot API bots cannot see presence)
and logs everything your account can observe in one group:

  - presence: every online/offline transition, however short
  - messages: who, when, text (optional), media flag
  - edits and deletions of messages
  - joins / leaves / kicks
  - username and display-name changes
  - periodic member snapshots

Everything goes to a local SQLite database. Optionally, events are also
forwarded to a private Telegram log group and to ntfy.sh push notifications
(controlled by LOG levels - see README.md).
"""

import asyncio
import logging
import os
import sqlite3
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.sessions import StringSession
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import ChatInviteAlready, UserStatusOnline, UserStatusOffline

# ----------------------------------------------------------------- config ---

def load_env_file(path=".env"):
    """Tiny .env loader so python-dotenv isn't required."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


load_env_file()

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
SESSION = os.environ.get("SESSION_NAME", "watchdog")
# If set (e.g. from a GitHub Actions secret), use this session string instead
# of a session file - required for CI hosting where no interactive login is
# possible. Generate it ONCE locally with generate_session.py.
SESSION_STRING = os.environ.get("SESSION_STRING", "")
# If > 0, delete presence/message/group events older than N days (keeps the
# database small when it has to be pushed around, e.g. on GitHub Actions).
PRUNE_DAYS = int(os.environ.get("PRUNE_DAYS", "0"))
GROUP = os.environ.get("GROUP", "")
DB_PATH = os.environ.get("DB_PATH", "presence.db")
SNAPSHOT_EVERY = int(os.environ.get("SNAPSHOT_EVERY", "30"))  # minutes, 0 = off

# --- forwarding ---------------------------------------------------------
LOG_GROUP = os.environ.get("LOG_GROUP", "")      # '' = disabled
TG_LOG_LEVEL = os.environ.get("TG_LOG_LEVEL", "everything")  # alerts|notable|everything
# Set (by the workflow) only when you chose a level in the "Run workflow"
# dropdown - that choice is authoritative and gets persisted to settings.
MANUAL_LEVEL = os.environ.get("MANUAL_LEVEL", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")     # '' = disabled
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_LEVEL = os.environ.get("NTFY_LEVEL", "alerts")  # alerts|everything

LOG_MESSAGE_TEXT = os.environ.get("LOG_MESSAGE_TEXT", "1") == "1"

# per-user board messages (online/offline history, one edited msg per member)
BOARD_EVERY = int(os.environ.get("BOARD_EVERY", "5"))       # flush cycle, seconds
BOARD_HISTORY = int(os.environ.get("BOARD_HISTORY", "12"))    # sessions kept per board

# --- PAT self-chain ---------------------------------------------------------
# The 5-minute cron relay is gone: ~30 min before the Actions job limit this
# run dispatches the NEXT run itself with PAT_TOKEN (GitHub refuses
# chain-dispatches via the built-in GITHUB_TOKEN), then exits cleanly. The
# new run has been queuing behind us the whole time (concurrency group,
# cancel-in-progress: false) so nothing is ever cancelled mid-flight.
CHAIN_AFTER_MIN = int(os.environ.get("CHAIN_AFTER_MIN", "330"))  # 0 = off
PAT_TOKEN = os.environ.get("PAT_TOKEN", "")

# --- alert thresholds ---------------------------------------------------
FLOOD_MSGS = int(os.environ.get("FLOOD_MSGS", "20"))      # messages ...
FLOOD_WINDOW = int(os.environ.get("FLOOD_WINDOW", "60"))   # ... within N seconds
ONLINE_ALERT_HOURS = int(os.environ.get("ONLINE_ALERT_HOURS", "24"))
DELETE_ALERT = int(os.environ.get("DELETE_ALERT", "10"))  # deletions within FLOOD_WINDOW

LEVELS = {"alerts": 0, "notable": 1, "everything": 2}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("watchdog")

# -------------------------------------------------------------------- db ----

SCHEMA = """
CREATE TABLE IF NOT EXISTS presence_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    ts      REAL    NOT NULL,
    online  INTEGER NOT NULL,
    source  TEXT    NOT NULL DEFAULT 'update'
);
CREATE INDEX IF NOT EXISTS idx_pe ON presence_events(user_id, ts);

CREATE TABLE IF NOT EXISTS messages (
    chat_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    ts          REAL    NOT NULL,
    text        TEXT,
    has_media   INTEGER DEFAULT 0,
    edit_count  INTEGER DEFAULT 0,
    last_edit_ts REAL,
    deleted     INTEGER DEFAULT 0,
    deleted_ts  REAL,
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_user ON messages(user_id, ts);

CREATE TABLE IF NOT EXISTS group_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL    NOT NULL,
    user_id INTEGER,
    kind    TEXT    NOT NULL,
    data    TEXT
);
CREATE INDEX IF NOT EXISTS idx_ge ON group_events(kind, ts);

CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    username       TEXT,
    display_name   TEXT,
    is_bot         INTEGER DEFAULT 0,
    first_observed REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

db = sqlite3.connect(DB_PATH, check_same_thread=False)
try:
    db.execute("PRAGMA journal_mode=WAL")      # fast + safe concurrent reads
    db.execute("PRAGMA synchronous=NORMAL")
except sqlite3.OperationalError:
    pass
db.executescript(SCHEMA)
db.commit()

SELF_ID = None
NAMES = {}  # uid -> readable name
LAST_STATE = {}  # uid -> (ts, online) of last accepted event (dedup guard)


def now_str():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def now_min():
    return fmt_tz(time.time())


DEFAULT_TZS = "Asia/Kolkata,UTC"


def parse_tz(spec):
    """'Asia/Kolkata,UTC' -> (['Asia/Kolkata', 'UTC'], [ZoneInfo, ...]).
    Invalid entries are dropped; falls back to UTC if none survive."""
    names, objs = [], []
    for name in (spec or "").split(","):
        name = name.strip()
        if not name:
            continue
        try:
            objs.append(ZoneInfo(name))
            names.append(name)
        except Exception:
            log.warning("ignoring invalid timezone %r", name)
    if not objs:
        names, objs = ["UTC"], [timezone.utc]
    return names, objs


def fmt_tz(ts, with_date=False):
    """One timestamp rendered in every configured zone, e.g.
    '14:30 IST · 09:00 UTC' (dates included when with_date=True).
    Set the zones via the TIMEZONES env var or live with /tz."""
    f = "%d %b %H:%M" if with_date else "%H:%M"
    out = []
    for tz in TZ_LIST:
        dt = datetime.fromtimestamp(ts, tz=tz)
        out.append("%s %s" % (dt.strftime(f), dt.tzname() or str(tz)))
    return " · ".join(out)


TZ_SPECS, TZ_LIST = parse_tz(os.environ.get("TIMEZONES", DEFAULT_TZS))


def name_of(uid):
    return NAMES.get(uid, "user#%s" % uid)


def load_names():
    for uid, username, display_name in db.execute(
            "SELECT user_id, username, display_name FROM users"):
        NAMES[uid] = ("@" + username) if username else (display_name or str(uid))


def remember_name(u):
    name = " ".join(p for p in (getattr(u, "first_name", None),
                                getattr(u, "last_name", None)) if p)
    NAMES[u.id] = ("@" + u.username) if u.username else (name or str(u.id))


def group_event(kind, data="", user_id=None):
    db.execute("INSERT INTO group_events (ts, user_id, kind, data) VALUES (?,?,?,?)",
               (time.time(), user_id, kind, data))
    db.commit()


def record_presence(user_id, online, ts, source="update"):
    """Insert a presence transition unless it is a duplicate or stale.
    Returns True if a row was actually inserted."""
    if user_id == SELF_ID:      # don't track ourselves (we look online 24/7)
        return False
    if user_id not in LAST_STATE:
        row = db.execute(
            "SELECT ts, online FROM presence_events WHERE user_id=? "
            "ORDER BY ts DESC LIMIT 1", (user_id,)).fetchone()
        LAST_STATE[user_id] = tuple(row) if row else None
    last = LAST_STATE.get(user_id)
    if last is not None:
        lts, lon = last
        if lon == online:            # same state again: no new information
            if ts > lts:             # but remember the fresher timestamp
                LAST_STATE[user_id] = (ts, online)
            return False
        if ts <= lts - 2:            # stale / out-of-order event
            return False
    db.execute(
        "INSERT INTO presence_events (user_id, online, ts, source) VALUES (?,?,?,?)",
        (user_id, online, ts, source))
    LAST_STATE[user_id] = (ts, online)
    db.commit()
    return True


def cleanup_bad_rows():
    """Repair for the pre-fix column-swap bug (ts/online were written to
    each other's columns) and Telegram's 1970 'was_online' garbage. Runs on
    every start; only malformed rows are touched."""
    cur = db.execute("DELETE FROM presence_events "
                     "WHERE ts < 1262304000 OR online NOT IN (0, 1)")
    db.commit()
    if cur.rowcount:
        log.info("removed %d malformed presence rows (pre-fix data)", cur.rowcount)


def upsert_user(u, detect_changes=True):
    if getattr(u, "bot", False) or getattr(u, "deleted", False):
        return
    name = " ".join(p for p in (getattr(u, "first_name", None),
                                getattr(u, "last_name", None)) if p)
    old = db.execute(
        "SELECT username, display_name FROM users WHERE user_id=?",
        (u.id,)).fetchone()
    if detect_changes and old:
        if (old[0] or "") != (u.username or ""):
            data = "%s -> %s" % (old[0] or "(none)", u.username or "(none)")
            group_event("username_change", data, u.id)
            if notifier:
                notifier.tg("%s | %s username: %s" % (now_min(), name_of(u.id), data), 1)
        if (old[1] or "") != (name or ""):
            data = "%s -> %s" % (old[1] or "(none)", name or "(none)")
            group_event("name_change", data, u.id)
            if notifier:
                notifier.tg("%s | %s name: %s" % (now_min(), name_of(u.id), data), 1)
    db.execute(
        """INSERT INTO users (user_id, username, display_name, is_bot, first_observed)
           VALUES (?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
             username=excluded.username,
             display_name=excluded.display_name""",
        (u.id, u.username, name, 0, time.time()))
    db.commit()
    remember_name(u)


def prune_old_data():
    if PRUNE_DAYS <= 0:
        return
    cutoff = time.time() - PRUNE_DAYS * 86400
    for table, col in (("presence_events", "ts"), ("messages", "ts"),
                       ("group_events", "ts")):
        db.execute("DELETE FROM %s WHERE %s < ?" % (table, col), (cutoff,))
    db.execute(
        "DELETE FROM users WHERE user_id NOT IN ("
        "  SELECT user_id FROM presence_events UNION "
        "  SELECT user_id FROM messages UNION "
        "  SELECT user_id FROM group_events WHERE user_id IS NOT NULL)")
    db.commit()


def get_setting(key, default=None):
    try:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
    except sqlite3.OperationalError:
        return default


def set_setting(key, value):
    db.execute("INSERT INTO settings (key, value) VALUES (?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


START_TS = time.time()
QUIET_SECONDS = int(os.environ.get("QUIET_SECONDS", "90"))  # skip board/log forwarding of the startup status burst


def short_dur(sec):
    sec = int(sec)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm" % (sec // 60)
    return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)


def record_status(user_id, status, source):
    if user_id == SELF_ID:
        return
    fresh = time.time() - START_TS > QUIET_SECONDS
    if isinstance(status, UserStatusOnline):
        if record_presence(user_id, 1, time.time(), source) and fresh:
            boards_touch(user_id)
    elif isinstance(status, UserStatusOffline):
        ts = time.time()
        if status.was_online:
            try:
                ts = status.was_online.timestamp()
            except Exception:
                pass
            if ts < 1262304000:   # Telegram sends 1970 for hidden statuses
                return              # - junk, do not record
            ts = min(ts, time.time())
        if record_presence(user_id, 0, ts, source) and fresh:
            boards_touch(user_id)
    # UserStatusRecently / LastWeek / LastMonth: hidden by privacy, untrackable.


# -------------------------------------------------------------- forwarding --

class Notifier:
    """Log-group output WITHOUT spam:

    - ONE rolling log message, edited in place; a new message is sent only
      after the current one passes 100 lines / ~3500 chars
    - ONE live-report message, re-edited every 10 minutes
    - alerts (floods, mass deletions, always-online) still get their own
      messages - they are rare and important
    - ntfy push for alerts, as before

    Levels: 0 = alerts, 1 = notable, 2 = everything (presence included).
    """

    MAX_LINES = 100
    MAX_CHARS = 3500

    def __init__(self, client, log_entity=None):
        self.client = client
        self.log_entity = log_entity
        self.tg_level = LEVELS.get(TG_LOG_LEVEL, 2) if LOG_GROUP else -1
        if LOG_GROUP and log_entity is None:
            self.tg_level = -1  # unresolved log group - disable forwarding
        self.ntfy_level = LEVELS.get(NTFY_LEVEL, 0) if NTFY_TOPIC else -1
        self.buffer = []
        self.log_lines = []      # content of the current rolling message
        self.log_msg_id = None
        self.report_msg_id = None
        self.ntfy_buf = []

    async def restore(self):
        """Continue editing the previous run's rolling / report messages
        (message ids persist in the database across handovers)."""
        if self.tg_level < 0:
            return
        for key, attr in (("log_msg_id", "log_msg_id"),
                          ("report_msg_id", "report_msg_id")):
            mid = int(get_setting(key, "0") or 0)
            if mid:
                setattr(self, attr, mid)
        if self.log_msg_id:
            try:
                msg = await self.client.get_messages(self.log_entity,
                                                     ids=self.log_msg_id)
                if msg:
                    self.log_lines = (msg.text or "").splitlines()[-self.MAX_LINES:]
            except Exception:
                self.log_msg_id = None
                self.log_lines = []

    # -- queueing ----------------------------------------------------------
    def tg(self, line, level=1):
        if self.tg_level < 0 or level > self.tg_level:
            return
        self.buffer.append(line)
        if level == 1 and self.ntfy_level >= 2:
            self.ntfy_buf.append(line)

    def presence(self, line):
        pass   # presence now goes to per-user boards (UserBoards below)

    async def alert(self, title, body):
        if self.tg_level >= 0:
            try:
                await self.client.send_message(
                    self.log_entity, "ALERT %s | %s" % (title, body),
                    parse_mode=None)
            except Exception:
                log.exception("alert send failed")
        if self.ntfy_level >= 0:
            await self._ntfy_post(title, body, priority="high")

    # -- sending -----------------------------------------------------------
    async def _ntfy_post(self, title, body, priority=None):
        def post():
            req = urllib.request.Request(
                "%s/%s" % (NTFY_SERVER, NTFY_TOPIC),
                data=body.encode("utf-8"),
                headers={"X-Title": title,
                         "Priority": priority or "default"})
            urllib.request.urlopen(req, timeout=10)
        try:
            await asyncio.get_event_loop().run_in_executor(None, post)
        except Exception as e:
            log.warning("ntfy failed: %s", e)

    async def _rolling_loop(self):
        while True:
            await asyncio.sleep(60)
            if not self.buffer:
                continue
            new = self.buffer[:]
            self.buffer.clear()
            try:
                merged = self.log_lines + new
                if (self.log_msg_id is None
                        or len(merged) > self.MAX_LINES
                        or len("\n".join(merged)) > self.MAX_CHARS):
                    # previous message full (or none yet): send a new one
                    msg = await self.client.send_message(
                        self.log_entity, "\n".join(new), parse_mode=None)
                    self.log_msg_id = msg.id
                    self.log_lines = new[-self.MAX_LINES:]
                    set_setting("log_msg_id", str(msg.id))
                else:
                    self.log_lines = merged
                    await self.client.edit_message(
                        self.log_entity, self.log_msg_id,
                        "\n".join(self.log_lines), parse_mode=None)
            except FloodWaitError as e:
                log.warning("flood-wait %ss - requeueing", e.seconds)
                self.buffer[:0] = new
                await asyncio.sleep(min(e.seconds + 5, 300))
            except Exception:
                log.exception("rolling log update failed")
                self.buffer[:0] = new
                self.log_msg_id = None   # start a fresh message next cycle
                self.log_lines = []

    async def _report_loop(self):
        first = True
        while True:
            await asyncio.sleep(30 if first else 600)
            first = False
            if self.tg_level < 0:
                continue
            try:
                import report as rep

                def build():
                    # separate connection + worker thread: never blocks the
                    # event loop that is busy capturing presence events
                    conn = sqlite3.connect(DB_PATH)
                    try:
                        return rep.compact_report(conn)
                    finally:
                        conn.close()

                text = await asyncio.get_event_loop().run_in_executor(
                    None, build)
                if self.report_msg_id:
                    await self.client.edit_message(
                        self.log_entity, self.report_msg_id, text,
                        parse_mode=None)
                else:
                    msg = await self.client.send_message(
                        self.log_entity, text, parse_mode=None)
                    self.report_msg_id = msg.id
                    set_setting("report_msg_id", str(msg.id))
            except Exception:
                log.exception("live report update failed")
                self.report_msg_id = None

    async def _ntfy_loop(self):
        # only used for NTFY_LEVEL=everything: batched notable lines
        while True:
            await asyncio.sleep(60)
            if not self.ntfy_buf:
                continue
            lines = self.ntfy_buf[:]
            self.ntfy_buf.clear()
            await self._ntfy_post("watchdog log (%d events)" % len(lines),
                                  "\n".join(lines[:20]))

    def start(self):
        if self.tg_level >= 0:
            client.loop.create_task(self._rolling_loop())
            client.loop.create_task(self._report_loop())
        if self.ntfy_level >= 0:
            client.loop.create_task(self._ntfy_loop())


class UserBoards:
    """One message per member, edited in place and fully REGENERATED from
    the database on every change - so it self-heals across restarts and
    6-hour handovers, and the uptime footer is always at the bottom:

        @abc - online 17:30 (34m)

        15:10→16:02 (52m)
        14:02→14:37 (35m)

        uptime 2h01m - 3 sessions - since 21 Sep

    Edits are coalesced (one flush cycle per few seconds) and capped per
    cycle to stay far below Telegram flood limits. Telegram refuses edits
    after ~48h, so a stale board automatically rolls over into a fresh
    message.
    """

    MAX_EDITS_PER_CYCLE = 15

    def __init__(self, client, log_entity):
        self.client = client
        self.log_entity = log_entity
        self.msg_ids = {}   # uid -> message id
        self.dirty = set()

    def enabled(self):
        return (self.log_entity is not None
                and notifier is not None and notifier.tg_level >= 2)

    def touch(self, uid):
        if self.enabled():
            self.dirty.add(uid)

    async def restore(self):
        try:
            for key, val in db.execute(
                    "SELECT key, value FROM settings WHERE key LIKE 'uboard:%'"):
                self.msg_ids[int(key.split(":", 1)[1])] = int(val)
            if self.msg_ids:
                log.info("restored %d per-user boards", len(self.msg_ids))
        except sqlite3.OperationalError:
            pass

    def _history(self, uid):
        tr = []
        for ts, on in db.execute(
                "SELECT ts, online FROM presence_events WHERE user_id=? "
                "ORDER BY ts", (uid,)):
            if tr and tr[-1][1] == on:
                continue
            tr.append((ts, on))
        sessions, i = [], 0
        while i < len(tr) - 1:
            if tr[i][1] == 1 and tr[i + 1][1] == 0:
                sessions.append((tr[i][0], tr[i + 1][0]))
                i += 2
            else:
                i += 1
        current_on = tr[-1][0] if tr and tr[-1][1] == 1 else None
        last_off = tr[-1][0] if tr and tr[-1][1] == 0 else None
        return sessions, current_on, last_off, (tr[0][0] if tr else None)

    def build_text(self, uid):
        sessions, cur_on, last_off, first_ts = self._history(uid)
        now = time.time()
        total = sum(b - a for a, b in sessions)
        if cur_on:
            total += now - cur_on
        n_sessions = len(sessions) + (1 if cur_on else 0)

        if cur_on:
            head = "%s - online %s (%s)" % (
                name_of(uid), fmt_tz(cur_on), short_dur(now - cur_on))
        elif last_off:
            head = "%s - last seen %s" % (
                name_of(uid),
                fmt_tz(last_off, with_date=(now - last_off) >= 86400))
        else:
            head = name_of(uid)

        lines = [head]
        shown = sessions[-BOARD_HISTORY:]
        if shown:
            lines.append("")
            for a, b in shown[::-1]:
                old = (now - a) >= 86400
                lines.append("%s → %s (%s)" % (
                    fmt_tz(a, with_date=old), fmt_tz(b, with_date=old),
                    short_dur(b - a)))
            if len(sessions) > BOARD_HISTORY:
                lines.append("(+%d older)" % (len(sessions) - BOARD_HISTORY))
        lines.append("")
        lines.append("uptime %s - %d sessions%s" % (
            short_dur(total), n_sessions,
            (" - since %s" % datetime.fromtimestamp(
                first_ts, tz=TZ_LIST[0]).strftime("%d %b")) if first_ts else ""))
        return "\n".join(lines)

    async def run(self):
        while True:
            await asyncio.sleep(BOARD_EVERY)
            if not self.dirty or not self.enabled():
                continue
            for uid in list(self.dirty)[:self.MAX_EDITS_PER_CYCLE]:
                self.dirty.discard(uid)
                try:
                    text = self.build_text(uid)
                    mid = self.msg_ids.get(uid)
                    if mid:
                        try:
                            await self.client.edit_message(
                                self.log_entity, mid, text, parse_mode=None)
                            continue
                        except MessageNotModifiedError:
                            continue
                        except FloodWaitError as e:
                            log.warning("board edit flood-wait %ss", e.seconds)
                            await asyncio.sleep(min(e.seconds + 5, 120))
                            self.dirty.add(uid)
                            continue
                        except Exception:
                            pass    # too old / deleted -> roll over to a new one
                    msg = await self.client.send_message(
                        self.log_entity, text, parse_mode=None)
                    self.msg_ids[uid] = msg.id
                    set_setting("uboard:%d" % uid, str(msg.id))
                except Exception:
                    log.exception("board update failed for %s", uid)
                    self.dirty.add(uid)


notifier = None  # created in main()
boards = None    # created in main()


def boards_touch(uid):
    if boards is not None:
        boards.touch(uid)


# ----------------------------------------------------------------- client ---

if SESSION_STRING:
    # CI mode: the session comes from a secret (already logged in, no prompt).
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
else:
    client = TelegramClient(SESSION, API_ID, API_HASH)


@client.on(events.UserUpdate)
async def on_user_update(event):
    record_status(event.user_id, event.status, "update")


FLOOD = {}   # uid -> deque of message timestamps
DELETES = {} # uid -> deque of deletion timestamps


def check_flood(uid):
    dq = FLOOD.setdefault(uid, deque())
    now = time.time()
    dq.append(now)
    while dq and now - dq[0] > FLOOD_WINDOW:
        dq.popleft()
    if len(dq) >= FLOOD_MSGS:
        dq.clear()  # fire once per burst
        asyncio.ensure_future(notifier.alert(
            "Message flood",
            "%s sent %d+ messages in under %ds" % (name_of(uid), FLOOD_MSGS, FLOOD_WINDOW)))


def check_deletes(uid, count):
    dq = DELETES.setdefault(uid, deque())
    now = time.time()
    for _ in range(count):
        dq.append(now)
    while dq and now - dq[0] > FLOOD_WINDOW:
        dq.popleft()
    if len(dq) >= DELETE_ALERT:
        dq.clear()
        asyncio.ensure_future(notifier.alert(
            "Mass deletion",
            "%s deleted %d+ messages in under %ds" % (name_of(uid), DELETE_ALERT, FLOOD_WINDOW)))


def bind_group_handlers(entity):
    chat_id = entity.id

    @client.on(events.NewMessage(chats=entity))
    async def on_new_message(event):
        uid = event.sender_id
        if not uid or uid == SELF_ID:
            return
        sender = await event.get_sender()
        if sender and not getattr(sender, "bot", False):
            upsert_user(sender, detect_changes=False)
        text = (event.raw_text or "")[:1000] if LOG_MESSAGE_TEXT else ""
        db.execute(
            """INSERT OR REPLACE INTO messages
               (chat_id, msg_id, user_id, ts, text, has_media)
               VALUES (?,?,?,?,?,?)""",
            (chat_id, event.message.id, uid, time.time(), text,
             1 if event.message.media else 0))
        db.commit()
        preview = ((event.raw_text or "")[:60]).replace("\n", " ") if LOG_MESSAGE_TEXT \
            else "(text not logged)"
        notifier.tg("%s | %s: %s" % (now_min(), name_of(uid), preview), 1)
        check_flood(uid)

    @client.on(events.MessageEdited(chats=entity))
    async def on_edit(event):
        new = (event.raw_text or "")[:1000] if LOG_MESSAGE_TEXT else ""
        cur = db.execute(
            "UPDATE messages SET text=?, edit_count=edit_count+1, last_edit_ts=? "
            "WHERE chat_id=? AND msg_id=?",
            (new, time.time(), chat_id, event.message.id))
        db.commit()
        if cur.rowcount:
            notifier.tg("%s | %s edited: %s" % (
                now_min(), name_of(event.sender_id),
                ((new or "(media)")[:40])), 1)

    @client.on(events.MessageDeleted(chats=entity))
    async def on_delete(event):
        n = 0
        for mid in event.deleted_ids:
            cur = db.execute(
                "UPDATE messages SET deleted=1, deleted_ts=? "
                "WHERE chat_id=? AND msg_id=? AND deleted=0",
                (time.time(), chat_id, mid))
            n += cur.rowcount
        db.commit()
        if n:
            uid_row = db.execute(
                "SELECT user_id FROM messages WHERE chat_id=? AND msg_id=?",
                (chat_id, event.deleted_ids[0])).fetchone()
            notifier.tg("%s | %s deleted %d msg(s)" % (
                now_min(), name_of(uid_row[0]) if uid_row else "unknown", n), 1)
            if uid_row:
                check_deletes(uid_row[0], n)

    @client.on(events.ChatAction(chats=entity))
    async def on_chat_action(event):
        uid = event.user_id
        user = await event.get_user() if uid else None
        if user:
            upsert_user(user, detect_changes=False)

        if event.user_joined or event.user_added:
            by = " (added by %s)" % name_of(event.added_by.id) if event.user_added and event.added_by else ""
            kind = "join"
            data = "joined%s" % by
        elif event.user_left:
            kind, data = "leave", "left"
        elif event.user_kicked:
            kind, data = "kicked", "kicked by %s" % (
                name_of(event.kicked_by.id) if event.kicked_by else "unknown")
        elif event.new_title:
            kind, data = "title", "title -> %s" % event.new_title
        else:
            return
        group_event(kind, data, uid)
        notifier.tg("%s | %s %s" % (now_min(), name_of(uid) if uid else "?", data), 1)


def bind_command_handlers(log_entity):
    """Live control from the private log group: send these commands there
    from your account (e.g. typed on your phone) and the bot obeys."""

    @client.on(events.NewMessage(chats=log_entity))
    async def on_command(event):
        if event.sender_id != SELF_ID:      # only the owner's own messages
            return
        text = (event.raw_text or "").strip()
        if not text.startswith("/"):
            return
        parts = text.split()          # keep case: IANA zones are case-sensitive
        cmd = parts[0].lower()

        if cmd == "/level" and len(parts) > 1 and parts[1].lower() in LEVELS:
            notifier.tg_level = LEVELS[parts[1].lower()]
            set_setting("tg_level", parts[1].lower())
            await event.reply(
                "Watchdog log level -> %s\n"
                "(saved in settings, survives 6h handovers)" % parts[1].lower())

        elif cmd == "/status":
            cur = {v: k for k, v in LEVELS.items()}.get(notifier.tg_level, "off")
            try:
                n_pres = db.execute("SELECT COUNT(*) FROM presence_events").fetchone()[0]
                n_msg = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                n_ge = db.execute("SELECT COUNT(*) FROM group_events").fetchone()[0]
                n_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            except sqlite3.OperationalError:
                n_pres = n_msg = n_ge = n_users = 0
            await event.reply(
                "Watchdog status\n"
                "level: %s  (/level alerts|notable|everything)\n"
                "this run started: %s\n"
                "members known: %d (boards: %d)\n"
                "logged: %d presence, %d messages, %d group events" % (
                    cur, get_setting("run_started", "?"),
                    n_users, len(boards.msg_ids) if boards else 0,
                    n_pres, n_msg, n_ge))

        elif cmd == "/report":
            import io
            import report as rep
            try:
                text_out, _ = rep.build_report(db)
                buf = io.BytesIO((text_out + "\n").encode("utf-8"))
                buf.name = "watchdog-report.md"
                await client.send_file(
                    log_entity, buf, caption="Watchdog report")
            except Exception:
                log.exception("report command failed")
                await event.reply("Report failed - check the Actions job log.")

        elif cmd == "/tz":
            global TZ_SPECS, TZ_LIST
            spec = " ".join(parts[1:]).replace(" ", "")
            if not spec:
                await event.reply(
                    "Timestamps are shown in: %s\n"
                    "Change with: /tz Asia/Kolkata,UTC\n"
                    "(any IANA names, e.g. Asia/Yerevan, Europe/London, America/New_York)"
                    % ", ".join(TZ_SPECS))
            else:
                names, objs = parse_tz(spec)
                if names == ["UTC"] and "UTC" not in spec.upper():
                    await event.reply(
                        "No valid timezone in %r - use IANA names like "
                        "Asia/Kolkata, UTC, Asia/Yerevan" % spec)
                else:
                    TZ_SPECS, TZ_LIST = names, objs
                    set_setting("timezones", ",".join(names))
                    await event.reply(
                        "Timestamps now shown in: %s\nExample: %s\n"
                        "(saved in settings, survives handovers)"
                        % (", ".join(names), fmt_tz(time.time())))

        elif cmd in ("/help", "/start"):
            await event.reply(
                "Watchdog commands (send them here, from your account):\n"
                "/level alerts|notable|everything - what to forward here\n"
                "/tz Asia/Kolkata,UTC - timezones shown on every timestamp\n"
                "/status - what am I doing\n"
                "/report - full spam-signal report as a file")


async def snapshot(entity):
    n = 0
    async for u in client.iter_participants(entity):
        n += 1
        upsert_user(u)
        record_status(u.id, u.status, "snapshot")
    log.info("snapshot: %d participants recorded", n)


async def presence_alerts():
    """Every 15 min: ONE aggregated alert about users online too long."""
    alerted = {}
    while True:
        await asyncio.sleep(900)
        try:
            rows = db.execute(
                "SELECT user_id, ts, online FROM presence_events pe "
                "WHERE id = (SELECT id FROM presence_events "
                "            WHERE user_id = pe.user_id ORDER BY ts DESC LIMIT 1)"
            ).fetchall()
            now = time.time()
            hits = []
            for uid, ts, online in rows:
                if online == 1 and now - ts >= ONLINE_ALERT_HOURS * 3600:
                    if now - alerted.get(uid, 0) > 48 * 3600:
                        alerted[uid] = now
                        hits.append((uid, (now - ts) / 3600))
            if hits:
                body = ["%d user(s) online for %d+ hours straight:" % (
                    len(hits), ONLINE_ALERT_HOURS)]
                for uid, hours in hits[:10]:
                    body.append("- %s (%.1f h)" % (name_of(uid), hours))
                if len(hits) > 10:
                    body.append("- ... and %d more" % (len(hits) - 10))
                await notifier.alert("Always-online users", "\n".join(body))
        except Exception:
            log.exception("presence alert check failed")


async def _find_entity(want):
    """get_entity by raw id fails on a fresh StringSession (no cached
    access_hashes), so fall back to scanning the account's dialogs."""
    try:
        return await client.get_entity(want)
    except Exception:
        pass
    if isinstance(want, int):
        async for d in client.iter_dialogs():
            if d.id == want or (want > 0 and d.id == -1000000000000 - want):
                return d.entity
    else:
        name = want.lstrip("@").lower()
        async for d in client.iter_dialogs():
            u = getattr(d.entity, "username", None) if d.entity else None
            if u and u.lower() == name:
                return d.entity
    return None


async def resolve_log_group():
    """Resolve LOG_GROUP to an entity (same StringSession caveat applies)."""
    if not LOG_GROUP:
        return None
    g = LOG_GROUP.strip().replace("https://t.me/", "@").replace("t.me/", "@")
    if g.lstrip("-").isdigit():
        want = int(g)
        cands = [want]
        if want > 0:
            cands.append(-1000000000000 - want)   # supergroup marked form
        for c in cands:
            ent = await _find_entity(c)
            if ent is not None:
                return ent
        log.warning("LOG_GROUP %s could not be resolved - "
                    "log-group forwarding disabled", LOG_GROUP)
        return None
    ent = await _find_entity(g if g.startswith("@") else "@" + g)
    if ent is None:
        log.warning("LOG_GROUP %s could not be resolved - "
                    "log-group forwarding disabled", LOG_GROUP)
    return ent


async def resolve_group():
    """Accepts @username, a numeric -100... id, a t.me/username link,
    or an invite link (t.me/+hash / t.me/joinchat/hash) for a group you
    have already joined (as an admin you have)."""
    g = GROUP.strip()

    # Full links: https://t.me/xyz, https://telegram.me/+hash, t.me/joinchat/xyz
    g = g.replace("telegram.me/", "t.me/")
    if "t.me/" in g:
        g = g.split("t.me/", 1)[1].split("?")[0].strip("/")

    if g.startswith("c/"):
        # private channel link like t.me/c/1234567890 - convert to internal id
        internal = g.split("c/", 1)[1].split("/")[0]
        if internal.isdigit():
            ent = await _find_entity(int("-100" + internal))
            if ent is not None:
                return ent

    if g.startswith("joinchat/") or g.startswith("+"):
        invite_hash = g.split("/")[-1].lstrip("+")
        result = await client(CheckChatInviteRequest(invite_hash))
        if isinstance(result, ChatInviteAlready):
            return result.chat
        raise SystemExit(
            "This invite link belongs to a group your account has not joined. "
            "Join it first with your account, then restart the bot.")

    if g.lstrip("-").isdigit():
        want = int(g)
    else:
        want = g
    ent = await _find_entity(want)
    if ent is None:
        raise SystemExit(
            "Could not resolve GROUP %r. Make sure your account is a member "
            "of the group, or use its @username / invite link instead." % GROUP)
    return ent


_chained = False   # True once the next run has been dispatched successfully


async def chain_next_run():
    """Dispatch the next workflow run with the PAT near the end of the job
    window, then stop this one cleanly so the final state push happens.
    On failure we simply keep watching until the hard timeout - the guard
    workflow resurrects the chain."""
    global _chained
    if not CHAIN_AFTER_MIN:
        return
    if not PAT_TOKEN:
        log.warning("PAT_TOKEN not set - self-chain disabled; add the secret "
                    "or the guard workflow has to resurrect every handover")
        return
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        log.warning("GITHUB_REPOSITORY not set - not running on Actions?")
        return
    await asyncio.sleep(CHAIN_AFTER_MIN * 60)
    import json as _json
    import urllib.request as _rq
    req = _rq.Request(
        "https://api.github.com/repos/%s/actions/workflows/watchdog.yml/dispatches" % repo,
        data=_json.dumps({"ref": "main"}).encode("utf-8"),
        headers={"Authorization": "Bearer " + PAT_TOKEN,
                 "Accept": "application/vnd.github+json"},
        method="POST")
    try:
        with _rq.urlopen(req, timeout=30) as r:
            log.info("next run dispatched (HTTP %s) - it queues behind us "
                     "and starts the moment we finish", r.status)
    except Exception as e:
        log.error("CHAIN DISPATCH FAILED (%r) - continuing until the hard "
                  "timeout; guard workflow will resurrect us", e)
        return
    _chained = True
    await client.disconnect()


async def main():
    global notifier, SELF_ID, boards

    if not (API_ID and API_HASH and GROUP):
        raise SystemExit("API_ID, API_HASH and GROUP must be set (see README.md)")

    await client.start()  # first run: asks for phone number + login code
    me = await client.get_me()
    SELF_ID = me.id
    log.info("logged in as %s", me.first_name)

    log_entity = await resolve_log_group()
    cleanup_bad_rows()
    # when monitoring actually began (settings survive handovers) - used by
    # the dashboard instead of the oldest event, because Telegram backfills
    # members' real last-seen timestamps (which can be months old)
    if not get_setting("monitor_since"):
        set_setting("monitor_since", str(int(time.time())))
    notifier = Notifier(client, log_entity)
    await notifier.restore()
    boards = UserBoards(client, log_entity)
    await boards.restore()

    # Log level priority: "Run workflow" dropdown choice > /level command
    # setting (persisted in the database) > the workflow's default.
    if MANUAL_LEVEL and MANUAL_LEVEL in LEVELS:
        set_setting("tg_level", MANUAL_LEVEL)
    saved_level = get_setting("tg_level")
    if (saved_level and saved_level in LEVELS and LOG_GROUP
            and notifier.tg_level >= 0):
        notifier.tg_level = LEVELS[saved_level]
        log.info("log level restored from settings: %s", saved_level)
    global TZ_SPECS, TZ_LIST
    saved_tz = get_setting("timezones")
    if saved_tz:
        TZ_SPECS, TZ_LIST = parse_tz(saved_tz)
        log.info("timezones restored from settings: %s", ", ".join(TZ_SPECS))
    log.info("timestamps shown in: %s", ", ".join(TZ_SPECS))
    set_setting("run_started",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    if log_entity is not None:
        bind_command_handlers(log_entity)
        log.info("log-group commands enabled: /level /status /report /help")

    entity = await resolve_group()
    log.info("watching group: %s", getattr(entity, "title", GROUP))
    log.info("destinations: log-group=%s (level %s), ntfy=%s (level %s)",
             LOG_GROUP or "OFF", TG_LOG_LEVEL if LOG_GROUP else "-",
             "ON" if NTFY_TOPIC else "OFF", NTFY_LEVEL if NTFY_TOPIC else "-")

    load_names()
    bind_group_handlers(entity)
    notifier.start()
    client.loop.create_task(boards.run())
    await snapshot(entity)
    client.loop.create_task(presence_alerts())

    if SNAPSHOT_EVERY > 0:
        async def resnap():
            while True:
                await asyncio.sleep(SNAPSHOT_EVERY * 60)
                prune_old_data()
                try:
                    await snapshot(entity)
                except FloodWaitError as e:
                    log.warning("flood wait %ss", e.seconds)
                    await asyncio.sleep(e.seconds + 5)
                except Exception:
                    log.exception("snapshot failed")
        client.loop.create_task(resnap())

    log.info("watchdog running (Ctrl+C to stop)")
    if CHAIN_AFTER_MIN:
        client.loop.create_task(chain_next_run())
    while True:
        try:
            await client.run_until_disconnected()
            if _chained:
                log.info("successor is queued - handing over now")
                break
            log.warning("disconnected - retrying in 10s")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            log.exception("connection error - retrying in 10s")
        await asyncio.sleep(10)


if __name__ == "__main__":
    client.loop.run_until_complete(main())
