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
  runs in a **self-chaining** loop: each job watches for ~5h30m, dispatches
  its own successor with the PAT token, and hands over with no dead time
  (~99% coverage).
- **Oracle Cloud Always Free VM** — best fully-continuous free option, but
  signup requires a credit/debit card for verification.
- A **cheap VPS** (~₹99-500/month).
- **Your own PC / old laptop** in a `tmux` session: `tmux new -s watchdog`,
  run `python bot.py`, detach with Ctrl+B then D.
- Kaggle does NOT work (sessions cap at ~12h, ~30h/week — daily blind gaps).

## Running 24/7 on GitHub Actions (free, no card needed)

GitHub Actions kills every job after 6 hours, so the repo ships with a
**self-chaining** workflow (`.github/workflows/watchdog.yml`):

1. There is **no cron** — crons fire late and a relay race once left the
   bot down. Instead, ~30 minutes before the job limit the bot itself
   dispatches the next run via the GitHub API using the `PAT_TOKEN`
   secret, then exits cleanly.
2. The new run queues behind the current one (concurrency group with
   `cancel-in-progress: false`) — **nothing is ever cancelled mid-run**.
   The moment the old job finishes (after its final state push), the new
   one starts, restores the encrypted database and takes over.
3. A `guard` workflow checks every 30 minutes that a watchdog run is alive
   (running or queued); if the chain ever breaks (expired PAT, failed
   dispatch, manual cancellation), it starts a fresh run automatically.

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
   pushed), `PAT_TOKEN` (**required** — it chains the runs, see below),
   and optionally `LOG_GROUP`, `NTFY_TOPIC`.
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

### PAT_TOKEN (required)

`PAT_TOKEN` is what keeps the watchdog alive without cron: GitHub refuses
workflow-dispatch calls made with the built-in `GITHUB_TOKEN`, so the bot
uses your personal access token to dispatch the next run of the chain, and
the `guard` + `keepalive` workflows use it too. **Prefer a fine-grained PAT
scoped to only this repository** with Contents read/write + Actions
read/write — a classic `repo`-scoped PAT also works but is far more powerful
than this needs. Set a long or no expiry. **If the PAT expires, the chain
stops and the guard cannot restart it either (it needs the same token) —
renew the PAT, then start the watchdog workflow manually.** For any other
chain break (failed dispatch, manual cancel) the guard covers it within
~30 minutes.

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
| `/tz Asia/Kolkata,UTC` | set which timezones every timestamp is shown in (multiple at once, e.g. `12:49 IST · 06:49 UTC`); any IANA names work; saved in the database |
| `/status` | current level, when this run started, how much data is logged |
| `/report` | generates the full spam-signal report and sends it into the chat as a `watchdog-report.md` file |
| `/help` | command list |

Levels: `alerts` = alert messages only; `notable` = alerts + messages,
edits, deletions, joins/leaves, name changes; `everything` = all of that
plus a per-member online/offline **board message** (see below).

**Anti-spam output design**: every member gets their **own board message**
that is edited in place each time they come online / go offline — showing
their recent sessions and a running total uptime that always sits at the
bottom. While a member is **online**, the head line also ticks live:
`online 17:30 (2m14s)` visibly counts up every few seconds (a local text
cache means Telegram edits only happen when the displayed text actually
changed, so settled sessions cost ~1 edit/min and nothing ever floods):

```
@abc - online 17:30 (34m)

15:10→16:02 (52m)
14:02→14:37 (35m)

uptime 2h01m - 3 sessions - since 21 Sep
```

Boards are fully regenerated from the database on every change, so they
survive restarts and 6-hour handovers, and Telegram's ~48h edit limit is
handled automatically by rolling over into a fresh message. Edits are
coalesced and rate-capped to stay far below flood limits. On top of that
there is ONE rolling log message for notable events (new one only after
100 lines) and a LIVE REPORT dashboard re-edited every 10 minutes with the
top-uptime members first (with their scores and reasons), plus stats.
Alerts (floods, mass deletions,
always-online) still arrive as their own messages since they are rare.

Performance hardening: SQLite runs in WAL mode, duplicate/stale presence
pushes are filtered before they touch the database, the live report is
built on a separate connection in a worker thread (never blocks event
capture), and the connection auto-retries on network errors.

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
