# Group Watchdog — review request

Thank you for taking the time to look at this project. This document explains
what it is, how it works, what we already know is fragile, and what we would
love your help with. The whole project is also in this zip — please review any
part of it.

## Purpose

A small Telegram group (~130 members) is being overrun by scripted accounts
and userbots. A human moderator cannot watch who is online 24/7, who floods
and deletes, who joined and left five times — so this project builds that
watchdog.

It is a **MTProto userbot** (running on the moderator's own account, via
Telethon) that passively observes one group and records everything it can
legally see:

- presence: every member's online / offline transitions (push-based,
  instant, plus a full participant snapshot every 30 minutes)
- message metadata: who sent what when, edits, deletions (message **text is
  off by default** — only counts and timing)
- group events: joins, leaves, kicks, username and display-name changes

From that data it computes per-member **spam / automation signals** (a 0–100
score with reasons, e.g. "online 24/7", "machine-regular offline gaps",
"message flood", "deleted 12 of 30 own messages", "join/leave churn").
Scores are heuristics only — the final judgment stays with the moderator.

## Design principles

1. **The moderator's account must not get banned.** Everything is rate-capped
   and edit-based: per-user boards are edited in place, one rolling log
   message, one dashboard message. Nothing is ever spammed to the group
   itself — the bot never writes to the watched group, it only reads.
2. **No paid infrastructure.** It runs 24/7 on GitHub Actions (free tier),
   self-chaining: ~30 minutes before GitHub's 6-hour job limit, the bot
   dispatches its own successor run via a PAT token, then exits cleanly. The
   successor has been queuing behind it the whole time (concurrency group,
   `cancel-in-progress: false`), so no run ever cancels another. A guard
   workflow resurrects the chain if it breaks.
3. **Nothing sensitive in the repo.** The repo is public code only. The
   Telegram session lives in an encrypted GitHub secret (StringSession), and
   the SQLite database is pushed to a `state` branch only as an AES-256-CBC
   (pbkdf2) encrypted gzip blob.

## What is in this zip

| File | Role |
|---|---|
| `bot.py` | the userbot: capture, storage, per-user boards, dashboard, alerts, live commands, PAT self-chain |
| `report.py` | scoring engine + full markdown report + CSV export + compact dashboard |
| `.github/workflows/watchdog.yml` | the always-on loop (self-chaining, no cron) |
| `.github/workflows/guard.yml` | 30-minute resurrection check |
| `.github/workflows/keepalive.yml` | defeats GitHub's 60-day schedule auto-disable |
| `sync_state.sh` | encrypted state push/pull between `state` branch and runners |
| `generate_session.py` | one-time login that produces the SESSION_STRING secret |
| `README.md` | user documentation |
| `SETUP-GUIDE.md` | step-by-step setup (Telegram, GitHub, Termux/PC) |

## Architecture in one minute

```
GitHub Actions runner (ubuntu-latest, ~5h30m per run)
 └─ bot.py (Telethon, StringSession)
     ├─ events → SQLite (WAL mode, 30-day pruning)
     ├─ per-user board messages ─┐   (one per member, backfilled on start
     ├─ one rolling log message  ├─  so nobody is invisible; edited in
     ├─ one LIVE REPORT dashboard ┘   place, timestamps multi-timezone)
     └─ alerts (flood / mass-delete / always-online) → own messages + ntfy push
 state: encrypted DB pushed to the `state` branch every ~32 min + on exit
 handover: bot dispatches its successor with PAT → exits → successor
 restores the encrypted DB and continues (message ids persist in the DB,
 so boards/logs/dashboard survive handovers)
```

### Feature highlights

- **Per-member board messages**: full online/offline session history and a
  running total uptime per member, edited in place; regenerated from the
  database so they self-heal across handovers; rolled over automatically
  when Telegram's ~48h edit limit hits.
- **Multi-timezone timestamps**: every stamp renders in all configured
  zones at once (default `13:49 IST · 08:19 UTC`), changeable live via
  `/tz Asia/Kolkata,UTC` (any IANA names) without restarting.
- **Live control**: `/level`, `/tz`, `/status`, `/report`, `/help` sent from
  the moderator's own account in the log group; all settings survive
  handovers.
- **Spam signals** (see `report.py`): 24/7 uptime, machine-regular offline
  gaps, no-sleep active hours, micro-sessions, message floods, delete
  ratios, join/leave churn — each with a 0–100 score and reasons.

## What we would like reviewed (please be harsh)

1. **Race conditions around the handover.** The chain assumes the successor
   run only starts after the predecessor's job fully completes (GitHub
   concurrency semantics). Is there any way both could run at once, and would
   that actually hurt (SQLite is in WAL mode, state pushes are force-pushes
   of a single-commit branch)?
2. **Telegram flood safety.** Boards flush at most 15 edits per 5-second
   cycle; the rolling log edits once per minute; the dashboard every 10
   minutes. Are we comfortably inside Telethon / Telegram limits for a
   personal account? What would you tighten?
3. **The scoring heuristics** in `report.py` (`score()` / `compute()`):
   24/7 uptime, coefficient-of-variation of offline gaps, active-hour
   coverage, micro-sessions, flood, delete-ratio, join/leave churn. Which of
   these produce false positives on real humans? What thresholds would you
   use?
4. **Data hygiene.** Presence events are deduplicated on write (same-state
   and stale/out-of-order events are dropped). Telegram backfills members'
   real last-seen timestamps (sometimes months old) as offline events — we
   keep those deliberately. Any pitfalls there?
5. **Security posture.** Public repo + encrypted DB on a `state` branch +
   session in a secret. What would you change?
6. **Anything else.** Dead code, error handling gaps, sqlite usage, asyncio
   misuse — anything that makes you wince.

## Features we are considering for "professional grade"

- member age / account metadata (no profile photo, default name, account
  created yesterday — via participant info we already fetch)
- similarity clustering: accounts that go online/offline within seconds of
  each other every time (likely one operator)
- heuristic confidence bands instead of a single 0–100 score
- scheduled daily summary report (post the top-10 suspects once a day)
- anomaly detection on message timing (Poisson burst detection)
- multi-group support (one bot, several watched groups)
- a small read-only web dashboard exported from the DB (no live server —
  static HTML from the report data, viewable on the phone)
- tesseract/OCR on optional media logging (currently off)
- export/backup commands (`/backup` sends the encrypted DB to a chat)

What else would you add? What would you cut?

## Honest limitations

- Members who hide their "last seen" are mostly invisible (Telegram privacy)
  — we log what Telegram exposes, nothing more.
- Handover gaps of ~2–3 minutes every ~5.5 hours while runs swap.
- The heuristics need ~a day of data before they mean anything.
- It is a single-user tool: no auth, no multi-tenant anything, by design.

## How to run it yourself

See `SETUP-GUIDE.md` — full walkthrough for Telegram API credentials,
SESSION_STRING generation, GitHub secrets, and starting the chain. Total
cost: zero. Total hardware: a phone.
