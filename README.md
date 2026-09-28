# Telegram Group Watchdog (userbot)

Runs on YOUR Telegram account and logs everything your account can observe in
one group, then analyses it for spam/automation signals.

## Why a userbot?

Telegram's Bot API does **not** expose other users' presence. Only a real user
account receives online/offline pushes for the members of its groups. This tool
logs into your account using MTProto (Telethon library) and listens passively.

## What gets logged (to SQLite)

| Event | Details |
|---|---|
| Presence | every online/offline transition, however short, with timestamps |
| Messages | who, when, text (first 1000 chars, optional), media flag |
| Edits | which message, when, new text |
| Deletions | which messages were deleted, when (post-then-delete is a spam pattern) |
| Joins/leaves/kicks | who, when, added-by/kicked-by |
| Username/name changes | old -> new, with timestamps |
| Member snapshots | every N minutes, as a safety net |

## What the report shows

`report.py` produces two sections:

- **A. Spam / automation signals** (the point of the tool): per-user bot score
  from presence patterns (online ~24/7, machine-regular gaps, no sleep
  pattern, scripted short sessions) and message behaviour (flood bursts,
  heavy deletions), join/leave churn, registered bots, deletion-heavy
  accounts.
- **B. Not directly spam-related, but logged (might come in handy)**:
  overall stats, top talkers, username/name change history, recent
  joins/leaves, members with hidden last seen. Full per-user presence details
  (total uptime, total offline time, longest session/gap, active hours).

## Forwarding to a Telegram log group + ntfy

Optional, configured in `.env`:

- `LOG_GROUP`: a private group you create; the watchdog forwards events there
  **from your account**. `TG_LOG_LEVEL` controls how much:
  `alerts` / `notable` (messages, edits, deletions, joins, name changes) /
  `everything` (adds every presence change, batched into one message per 5
  minutes).
- `NTFY_TOPIC`: push notifications via [ntfy.sh](https://ntfy.sh). Install the
  app (Android/iOS) or open `https://ntfy.sh/<topic>` in a browser and
  subscribe to the topic. `NTFY_LEVEL=alerts` sends flood / always-online /
  mass-deletion alerts; `everything` also pushes notable events (batched
  once a minute).

**Choose a long random ntfy topic name** — anyone who knows the topic can
read the notifications.

**Volume warning**: `TG_LOG_LEVEL=everything` in a busy group can mean
hundreds of forwarded messages per day sent from your account. That is the one
behaviour that genuinely risks Telegram flood-limits — consider `notable` if
you don't truly need live presence in the log group (it is always in the
database anyway, and presence is batched separately).

## "Will I look online 24/7? Will Telegram ban me?"

- **Yes, while the bot runs your account appears online around the clock.**
  Any connected session (including official desktop clients people leave
  running for days) keeps you marked online.
- **Being online is not a ban trigger.** Telegram bans for behaviour —
  flooding, mass-adding, bulk scraping, spam. Passive listening plus batched
  log messages is gentle. This bot deliberately never sends more than a few
  messages per minute.
- You **cannot** hide your own always-online status without breaking the
  tool: setting your "Last seen & online" privacy to Nobody is reciprocal —
  Telegram then hides *everyone else's* presence from you too.
- Real safety rules: enable 2FA, never share the `.session` file, don't run
  this on a brand-new account, and don't run other aggressive userbots on the
  same account.

## Hard limitations (read this)

1. **Members who hide their "Last Seen & Online" privacy from you cannot be
   tracked** — by anyone, with any tool. They appear in section B of the
   report. A hidden last seen is itself a (weak) spam signal, but plenty of
   normal users hide it too.
2. For very large channels, `iter_participants` may only return a subset
   unless you are an admin there. Being a moderator/admin (as you already
   are) is usually enough.
3. Running a userbot is technically against Telegram's ToS. Passive monitoring
   like this is gentle and widely done, but keep it low-key.

## Setup

1. Python 3.9+ and:
   ```
   pip install -r requirements.txt
   ```
2. Get API credentials at <https://my.telegram.org> → "API development tools"
   (login with your phone number, create an app, copy API_ID and API_HASH).
3. Copy `.env.example` to `.env` and fill it in. For `GROUP` you can paste
   any of: `@username`, a public `https://t.me/username` link, the invite
   link (`https://t.me/+AbCdEf123`) — works because your account has already
   joined, or the numeric id `-1001234567890`.
4. Create a private group for logs (if you want them), add your account, put
   its id/username in `LOG_GROUP`.
5. First run (asks for your phone number and the login code once):
   ```
   python bot.py
   ```
   Leave it running for at least a few days — a week gives much better
   judgements.

## Generating a report

```
python report.py                      # print to console
python report.py -o report.md         # markdown file
python report.py --csv users.csv      # flat table for Excel
```

## Where to host it

This needs an **always-on process** — if it's not running, you miss events.

- **GitHub Actions (free, no card needed)** — see the section below. The bot
  runs in a relay: each job watches for ~5h48m, saves state, and the next
  scheduled run takes over within minutes (~98-99% coverage).
- **Oracle Cloud Always Free VM** — best fully-continuous free option, but
  signup requires a credit/debit card for verification.
- A **cheap VPS** (~₹99-500/month).
- **Your own PC / old laptop** in a `tmux` session: `tmux new -s watchdog`,
  run `python bot.py`, detach with Ctrl+B then D.
- Kaggle does NOT work (sessions cap at ~12h, ~30h/week — daily blind gaps).

## Running 24/7 on GitHub Actions (free, no card needed)

GitHub Actions kills every job after 6 hours, so the repo ships with a
**relay** workflow (`.github/workflows/watchdog.yml`):

1. A schedule ticks every 5 minutes. Each run checks a `heartbeat` on the
   `state` branch — if a healthy runner is already watching, it exits.
2. Otherwise it restores the saved database, runs the bot for ~5h48m, and
   stops itself cleanly. The next tick takes over within minutes.
3. Handover gaps are ~5-6 minutes every ~6 hours; snapshots on startup
   recover the current state of every member after each gap.

**Setup (one time):**

1. Create a **public** repo (private repos only get 2,000 free Actions
   minutes/month — 24/7 needs ~43,000) containing this code, plus the
   workflow file, `sync_state.sh` and `generate_session.py`.
2. On your own computer: `pip install telethon`, then
   `python generate_session.py` — log in with phone + code, copy the
   SESSION_STRING it prints (this replaces the session file on CI).
3. In the repo: Settings → Secrets and variables → Actions → add secrets:
   `API_ID`, `API_HASH`, `SESSION_STRING`, `GROUP` (link/@username/id),
   `DB_PASS` (any long random string — encrypts the database before it is
   pushed), and optionally `LOG_GROUP`, `NTFY_TOPIC`.
4. Actions tab → enable the **watchdog** workflow → run it
   ("Run workflow") once manually. Within a minute it should print
   "watchdog running". The `state` branch appears with the encrypted DB.

Security model: the repo only ever contains code; your session lives in an
encrypted GitHub secret; the database is pushed only as an AES-256-encrypted
gzip blob (`sync_state.sh`), never in plaintext.

### Keeping the schedule alive past 60 days

GitHub auto-disables **scheduled** workflows in public repos after 60 days
without repository activity, and what counts is commits on the default
branch — the watchdog's state commits go to the `state` branch, so don't
rely on them. The `keepalive` workflow (`.github/workflows/keepalive.yml`)
solves this: it runs monthly, pushes an empty commit to `main` and
re-enables both workflows via the API (which resets the 60-day timer). It
needs one secret:

- `PAT_TOKEN` — a personal access token that can push to this repo
  (classic PAT with `repo` scope works; or a fine-grained PAT scoped to
  this repository with Contents read/write).

If the workflow ever shows as disabled anyway, open the Actions tab and hit
"Enable workflow" — you'll also see a "This workflow will be disabled soon"
banner as a warning.

### Controlling the watchdog live, from the log group

The "Run workflow" button offers a log-level dropdown (default: `everything`).
You can also change it (and more) at runtime by sending commands **in your
private log group, from the same account** — the bot obeys instantly:

| Command | Effect |
|---|---|
| `/level everything` (or `notable` / `alerts`) | change what gets forwarded to the log group; saved in the database, so it survives 6-hour handovers |
| `/status` | current level, when this run started, how much data is logged |
| `/report` | generates the full spam-signal report and sends it into the chat as a `watchdog-report.md` file |
| `/help` | command list |

Levels: `alerts` = floods/always-online/mass-deletions only; `notable` =
alerts + messages, edits, deletions, joins/leaves, name changes; `everything`
= all of that plus every online/offline (batched into one message per 5 min).

## Security notes

- The `.session` file created on first login **is your account**. Never
  commit it to git, never share it. Delete it to de-authorise the program.
- Keep `.env` out of git (add both to `.gitignore`).
- Enable 2FA on your Telegram account before running this.

## Files

- `bot.py` — the watchdog (run this continuously)
- `report.py` — analysis + report generator (run anytime)
- `.env.example` — config template
- `requirements.txt` — dependencies (just Telethon)

## Upgrading from v1

v2 uses a new database schema (message text, edits, deletions, group events).
Either delete the old `presence.db` or set `DB_PATH` to a new filename.
