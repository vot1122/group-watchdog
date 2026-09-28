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

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
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
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")     # '' = disabled
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_LEVEL = os.environ.get("NTFY_LEVEL", "alerts")  # alerts|everything

LOG_MESSAGE_TEXT = os.environ.get("LOG_MESSAGE_TEXT", "1") == "1"

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
"""

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript(SCHEMA)
db.commit()

SELF_ID = None
NAMES = {}  # uid -> readable name


def now_str():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


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
    if user_id == SELF_ID:      # don't track ourselves (we look online 24/7)
        return
    db.execute(
        "INSERT INTO presence_events (user_id, ts, online, source) VALUES (?,?,?,?)",
        (user_id, online, ts, source))
    db.commit()


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
                notifier.tg("[%s] %s changed username: %s" % (now_str(), name_of(u.id), data), 1)
        if (old[1] or "") != (name or ""):
            data = "%s -> %s" % (old[1] or "(none)", name or "(none)")
            group_event("name_change", data, u.id)
            if notifier:
                notifier.tg("[%s] %s changed name: %s" % (now_str(), name_of(u.id), data), 1)
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


def record_status(user_id, status, source):
    if user_id == SELF_ID:
        return
    if isinstance(status, UserStatusOnline):
        record_presence(user_id, 1, time.time(), source)
        if notifier:
            notifier.presence("[%s] %s -> online" % (now_str(), name_of(user_id)))
    elif isinstance(status, UserStatusOffline):
        ts = status.was_online.timestamp() if status.was_online else time.time()
        record_presence(user_id, 0, ts, source)
        if notifier:
            notifier.presence("[%s] %s -> offline" % (now_str(), name_of(user_id)))
    # UserStatusRecently / LastWeek / LastMonth: hidden by privacy, untrackable.


# -------------------------------------------------------------- forwarding --

class Notifier:
    """Forwards log lines to the Telegram log group and ntfy.

    Levels: 0 = alerts, 1 = notable, 2 = presence (everything).
    Batching is intentional: sending one message per event would hit
    Telegram flood limits in an active group.
    """

    def __init__(self, client, log_entity=None):
        self.client = client
        self.log_entity = log_entity
        self.tg_level = LEVELS.get(TG_LOG_LEVEL, 2) if LOG_GROUP else -1
        if LOG_GROUP and log_entity is None:
            self.tg_level = -1  # unresolved log group - disable forwarding
        self.ntfy_level = LEVELS.get(NTFY_LEVEL, 0) if NTFY_TOPIC else -1
        self.pending = []        # alerts + notable
        self.presence_buf = []   # presence lines (batched slowly)
        self.ntfy_buf = []       # notable lines for ntfy (only if everything)

    # -- queueing ----------------------------------------------------------
    def tg(self, line, level=1):
        if self.tg_level < 0 or level > self.tg_level:
            return
        self.pending.append(line)
        if level == 1 and self.ntfy_level >= 2:
            self.ntfy_buf.append(line)

    def presence(self, line):
        if self.tg_level < 2:
            return
        self.presence_buf.append(line)

    async def alert(self, title, body):
        self.tg("ALERT %s | %s" % (title, body), 0)
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

    async def _flush_loop(self, buf, interval):
        while True:
            await asyncio.sleep(interval)
            if not buf:
                continue
            lines = buf[:]
            buf.clear()
            try:
                text = "\n".join(lines)
                for i in range(0, len(text), 4000):
                    await self.client.send_message(self.log_entity, text[i:i + 4000])
            except FloodWaitError as e:
                log.warning("flood-wait %ss - requeueing log batch", e.seconds)
                buf[:0] = lines
                await asyncio.sleep(min(e.seconds + 5, 300))
            except Exception:
                log.exception("log-group send failed - dropping batch")

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
            client.loop.create_task(self._flush_loop(self.pending, 10))
            client.loop.create_task(self._flush_loop(self.presence_buf, 300))
        if self.ntfy_level >= 0:
            client.loop.create_task(self._ntfy_loop())


notifier = None  # created in main()


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
        preview = ((event.raw_text or "")[:80]).replace("\n", " ") if LOG_MESSAGE_TEXT \
            else "(text not logged)"
        notifier.tg("[%s] msg %s: %s" % (now_str(), name_of(uid), preview), 1)
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
            notifier.tg("[%s] edited %s: %s" % (
                now_str(), name_of(event.sender_id),
                (new[:60] or "(media)")), 1)

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
            notifier.tg("[%s] deleted %d message(s) by %s" % (
                now_str(), n, name_of(uid_row[0]) if uid_row else "unknown"), 1)
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
        notifier.tg("[%s] %s %s" % (now_str(), name_of(uid) if uid else "?", data), 1)


async def snapshot(entity):
    n = 0
    async for u in client.iter_participants(entity):
        n += 1
        upsert_user(u)
        record_status(u.id, u.status, "snapshot")
    log.info("snapshot: %d participants recorded", n)


async def presence_alerts():
    """Every 15 min: alert about users online continuously for too long."""
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
            for uid, ts, online in rows:
                if online and now - ts >= ONLINE_ALERT_HOURS * 3600:
                    if now - alerted.get(uid, 0) > 48 * 3600:
                        alerted[uid] = now
                        await notifier.alert(
                            "Always-online user",
                            "%s has been online for %.1f hours straight" % (
                                name_of(uid), (now - ts) / 3600))
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


async def main():
    global notifier, SELF_ID

    if not (API_ID and API_HASH and GROUP):
        raise SystemExit("API_ID, API_HASH and GROUP must be set (see README.md)")

    await client.start()  # first run: asks for phone number + login code
    me = await client.get_me()
    SELF_ID = me.id
    log.info("logged in as %s", me.first_name)

    log_entity = await resolve_log_group()
    notifier = Notifier(client, log_entity)
    entity = await resolve_group()
    log.info("watching group: %s", getattr(entity, "title", GROUP))
    log.info("destinations: log-group=%s (level %s), ntfy=%s (level %s)",
             LOG_GROUP or "OFF", TG_LOG_LEVEL if LOG_GROUP else "-",
             "ON" if NTFY_TOPIC else "OFF", NTFY_LEVEL if NTFY_TOPIC else "-")

    load_names()
    bind_group_handlers(entity)
    notifier.start()
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
    await client.run_until_disconnected()


if __name__ == "__main__":
    client.loop.run_until_complete(main())
