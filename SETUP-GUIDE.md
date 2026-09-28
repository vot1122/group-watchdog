# Watchdog — Full Setup Guide (start to 24/7)

This guide takes you from zero to a running watchdog in about an hour. Follow
Parts 1-2 once on your own computer (that's mandatory — it's also your test
run), then pick ONE option in Part 3 to run it 24/7.

**What you need before starting:** your Telegram account, a phone number, an
email address, and (for the free cloud option) a debit/credit card for
verification only.

---

## Part 0 — Mobile-only setup (Android: Termux + browser, no PC)

Everything in this guide works from a phone. Only one step needs Termux
(generating your session string, once). For all websites below, open them in
Chrome with "Desktop site" ticked (⋮ menu) — GitHub and my.telegram.org hide
some buttons on the mobile layout.

### 0.1 Install Termux (one time)

Install Termux from **F-Droid** (f-droid.org) or from GitHub releases — the
Play Store version is outdated and broken on newer Androids. Just search
"Termux F-Droid" in your browser, install the F-Droid store app, then Termux
from inside it.

### 0.2 Telegram prep (all inside the Telegram app + browser)

Do Part 1 below, all from your phone: 2FA in the app, my.telegram.org in the
browser (log in with your phone number; the code arrives inside the Telegram
app), create your log group in the app, install the ntfy app from the Play
Store and subscribe to a long random topic.

### 0.3 Add the GitHub secrets (mobile browser)

Open https://github.com/vot1122/group-watchdog → Settings → Secrets and
variables → Actions → "New repository secret". Add, one by one:

- `API_ID` — from my.telegram.org
- `API_HASH` — from my.telegram.org
- `DB_PASS` — any long random string you invent
- `GROUP` — your group's link / @username / -100... id
- (optional) `LOG_GROUP`, `NTFY_TOPIC`
- (optional but recommended) `PAT_TOKEN` — a personal access token with
  access to this repo (classic PAT with `repo` scope, or fine-grained with
  Contents read/write on this repo only). Used ONLY by the `keepalive`
  workflow to keep GitHub's 60-day schedule auto-disable from killing the
  watchdog. Without it the bot still runs - you'd just need to re-enable the
  workflow manually every 60 days.

Leave `SESSION_STRING` for the next step.

### 0.4 Generate SESSION_STRING in Termux (once)

Open Termux and run:

```
pkg update -y && pkg install -y python git
git clone https://github.com/vot1122/group-watchdog
cd group-watchdog
pip install telethon
python generate_session.py
```

Enter API_ID, API_HASH, then your phone number (+91...) and the code
Telegram sends to your app. The script prints your SESSION_STRING and saves
it to session.txt. Copy it either way:

- easy way: `pkg install termux-api` + install the "Termux:API" app
  (F-Droid), then run `cat session.txt | termux-clipboard-set`
- manual way: `cat session.txt`, long-press the line, Select all → Copy

Now add it as the final secret `SESSION_STRING` in the repo settings
(step 0.3).

### 0.5 Start the watchdog (mobile browser)

Repo → Actions tab → select "watchdog" → "Run workflow" → Run. Open the
running job and wait for `watchdog running`. Done — after this it relays
itself around GitHub's 6-hour job limit automatically (see Option A in
Part 3 for what to expect in the Actions list).

### 0.6 Reports on your phone (any time)

1. Browser → repo → Code → branch dropdown → `state` → tap `presence.db.enc`
   → download it (lands in your Downloads folder).
2. Termux:

```
termux-setup-storage                     # allow file access (once)
pkg install -y openssl
cp ~/storage/downloads/presence.db.enc .
openssl enc -aes-256-cbc -pbkdf2 -d -in presence.db.enc \
  -pass pass:YOUR_DB_PASS | gunzip > presence.db
python report.py --db presence.db -o report.md
cat report.md                           # or: termux-open report.md
```

### 0.7 Optional: run the bot in Termux instead of GitHub Actions

Your phone can also BE the server (free, fully continuous):

```
cd group-watchdog
termux-wake-lock
python bot.py
```

Then in Android settings: Settings → Apps → Termux → Battery → Unrestricted,
so Android doesn't kill it. Honest downsides: Android may still stop it after
days, phone reboots kill it, and it drains battery — the GitHub relay is more
reliable long-term. But for a trial week, it's the simplest option of all,
and the same `session.txt`/`watchdog.session` works for both.

---

## Part 1 — One-time Telegram prep (15 minutes, on your phone/PC)

### 1.1 Turn on two-step verification on your account
Telegram app → Settings → Privacy and Security → Two-Step Verification → set
a password. Do this BEFORE running the bot; it protects your account and the
session the bot will create.

### 1.2 Get your API keys
1. Open https://my.telegram.org in a browser and log in with your phone
   number (Telegram sends you a login code in the app).
2. Click **API development tools**.
3. Fill the form: App title = anything ("watchdog"), Short name = anything,
   platform = can stay as is, URL = leave blank.
4. Create the app. You'll see **App api_id** (a number) and **App api_hash**
   (a long string). Save both — they go in your `.env` file later.

### 1.3 Create your log group (optional but recommended)
1. In Telegram: New Group → add only yourself → name it something like
   "watchdog logs".
2. In that group, open the header → click the group name → it's private, so
   you have no @username. Instead, note the numeric id: forward any message
   from this group to the bot **@userinfobot** and it replies with the chat
   id (looks like `-1001234567890`). That number is your `LOG_GROUP` value.
   (If you make the group public it also gets a @username you can use.)

### 1.4 Set up ntfy push notifications (optional but recommended)
1. Install the **ntfy** app (Android: Play Store / F-Droid; iOS: App Store).
2. In the app: **+ Subscribe** → choose a topic name. Make it LONG and
   RANDOM, e.g. `watchdog-b7k2m9q4-ananya` — anyone who knows the topic name
   can read the notifications, so treat it like a password.
3. That topic name is your `NTFY_TOPIC` value.
4. Test it: on a computer open
   `https://ntfy.sh/YOUR-TOPIC-NAME` in a browser — whatever appears there
   is what your phone will receive.

---

## Part 2 — First run on your own computer (30 minutes)

Do this even if you plan to host elsewhere — it's your test run, and the
first login is much easier on a desktop.

### 2.1 Install Python
- **Windows:** download from https://python.org/downloads, run the installer
  and **tick "Add python.exe to PATH"** on the first screen. Verify: open
  Command Prompt → `python --version` → should print Python 3.x.
- **Mac:** Python 3 is usually preinstalled; otherwise `brew install python`
  or install from python.org.
- **Linux:** `sudo apt install python3 python3-pip python3-venv` (Debian/Ubuntu).

### 2.2 Get the files
Unzip `presence-monitor.zip` into a folder, e.g. `C:\watchdog` (Windows) or
`~/watchdog` (Mac/Linux). You should see `bot.py`, `report.py`,
`requirements.txt`, `.env.example`, `README.md`.

### 2.3 Install the dependency
Open a terminal in that folder (Windows: type `cmd` in the folder's address
bar and press Enter). Then:

```
python -m pip install -r requirements.txt
```

(On Mac/Linux use `python3`.) If your system complains about
externally-managed environments, use a venv instead:

```
python3 -m venv venv
source venv/bin/activate        # Mac/Linux
venv\Scripts\activate           # Windows
pip install -r requirements.txt
```

### 2.4 Configure
Copy `.env.example` to a new file named `.env` in the same folder, and fill in:
`API_ID` and `API_HASH` from step 1.2, `GROUP` = paste your group's link or
@username or `-100...` id (all formats work), and optionally `LOG_GROUP` and
`NTFY_TOPIC` from steps 1.3-1.4.

If you don't want message text stored, set `LOG_MESSAGE_TEXT=0`.
If the group is very active, set `TG_LOG_LEVEL=notable` (see README).

### 2.5 First run and login
```
python bot.py
```
It will ask (once, ever):
- your phone number in international format (`+91...`),
- the login code Telegram sends to your app,
- your 2FA password (if you set one in 1.1).

Then you should see:
```
INFO logged in as <your name>
INFO watching group: <your group name>
INFO snapshot: 257 participants recorded
INFO watchdog running (Ctrl+C to stop)
```
Leave it running for a while. Check your log group and ntfy — test events
should start appearing. A file `watchdog.session` now exists in your folder:
**it IS your logged-in account — never share or upload it.**

### 2.6 Generate a report (any time)
```
python report.py -o report.md
```
Open `report.md` to see section A (spam signals) and B (everything else).

**Success criteria for Part 2:** snapshot line shows participants, no errors
in the console, events appear in the log group / ntfy. If something fails,
see Troubleshooting at the end. Once this works, move to Part 3.

---

## Part 3 — Running 24/7: pick ONE option

The bot only sees what happens while it runs. Compare the options:

| Option | Cost | Reliability | Effort |
|---|---|---|---|
| A. GitHub Actions (relay) | Free, no card | ~98-99% (short handover gaps) | Low |
| B. Your own PC/laptop | Free | Power cuts, reboots | Lowest |
| C. Cheap Indian VPS | ₹99-500/month | Excellent | Medium |
| D. Oracle Cloud Always Free | Free forever, needs a card to verify | Good (see caveat) | Highest |
| ~~Kaggle~~ | — | 12h sessions, ~30h/week cap | — |

**If you have no card and no budget, Option A (GitHub Actions) is your
answer** — the repo is built so the bot relays itself around GitHub's 6-hour
job limit. Kaggle remains crossed out: notebook sessions stop after ~12
hours with a weekly quota, giving real daily blind gaps that no trick fixes.

### Option A — GitHub Actions (free, no card, ~98-99% coverage)

How the relay works: a schedule ticks every 5 minutes; each run checks a
heartbeat pushed to a `state` branch — if a healthy runner is already
watching, it exits within seconds; otherwise it restores the encrypted
database, runs the bot for ~5h48m, and hands over to the next tick. The
handover gap is ~5-6 minutes every ~6 hours; startup snapshots recover every
member's current state after each gap.

1. **The repo is already created for you** (public, with the workflow
   `.github/workflows/watchdog.yml`, `sync_state.sh` and
   `generate_session.py` inside). You could also build it yourself from the
   `presence-monitor` zip.
2. **Generate your session string ONCE, on your own computer:**
   ```
   pip install telethon
   python generate_session.py
   ```
   Enter your API_ID, API_HASH, phone number (+91...) and the code Telegram
   sends to your app. It prints a long `SESSION_STRING` line — copy it.
3. **Add the secrets** — repo page → Settings → Secrets and variables →
   Actions → "New repository secret" for each:
   - `API_ID` — from my.telegram.org
   - `API_HASH` — from my.telegram.org
   - `SESSION_STRING` — the long line from step 2
   - `GROUP` — your group's link / @username / -100... id
   - `DB_PASS` — any long random string you invent (it encrypts the database
     before it is stored in the repo)
   - optional: `LOG_GROUP`, `NTFY_TOPIC`
4. **Start it:** repo → Actions tab → select "watchdog" → "Run workflow" →
   Run. Click the running job and watch the log — you want to see
   `watchdog running`. After ~10 minutes, a `state` branch appears
   (encrypted database + heartbeat).
5. **Verify the relay:** after ~5h50m the first job ends; the next scheduled
   run should start within minutes and print `watchdog running` again.
   The Actions list shows short green runs (skip) plus long green runs
   (actual watching) — that pattern is healthy.
6. **Get reports:** easiest via the Actions job log, or pull the DB:
   download `presence.db.enc` from the `state` branch, then on your PC:
   ```
   openssl enc -aes-256-cbc -pbkdf2 -d -in presence.db.enc -pass pass:YOUR_DB_PASS | gunzip > presence.db
   python report.py --db presence.db -o report.md
   ```

Notes and honest caveats:
- The repo MUST stay public (private repos get only 2,000 free Actions
  minutes/month; 24/7 needs ~43,000). Nothing sensitive is in it: your
  session and keys are GitHub secrets, the database is AES-encrypted.
- Your account will be online almost 24/7 minus the small handover gaps.
- GitHub auto-disables schedules after 60 days with no commits — the state
  pushes count as activity, so this normally never triggers; if it ever
  does, just re-enable the workflow in the Actions tab.
- The workflow sets `LOG_MESSAGE_TEXT=0` and `PRUNE_DAYS=30` to keep the
  encrypted DB small (it gets pushed every ~30 minutes). Change these in
  `.github/workflows/watchdog.yml` if you want text logged.

### Option A — your own computer (free, easiest)

The bot uses almost no CPU, so any PC works. The only enemies are sleep and
power cuts.

1. Windows: Settings → System → Power → set **Sleep = Never** (screen can
   still turn off; that's fine).
2. Make it auto-start after a reboot — create a file `start-watchdog.bat` in
   the bot folder:
   ```
   @echo off
   cd /d C:\watchdog
   python bot.py
   ```
   (adjust the path to wherever your folder is). Press Win+R, type
   `shell:startup`, Enter, and put a copy of the .bat file in that folder.
   The bot now restarts whenever Windows does.
3. If you use a venv, change the last line to
   `venv\Scripts\python bot.py`.

Downsides: an inverter/UPS helps in India's power cuts; if the PC is off for
2 hours, you lose 2 hours of presence events (snapshots partially compensate
— see README).

### Option B — Oracle Cloud Always Free (free forever, most steps)

Oracle gives a permanently free VM (up to 2 ARM CPU cores + 12 GB RAM) —
more than enough for this bot. Verification needs a debit/credit card, but
it is not charged unless you manually upgrade the account.

**Caveat:** Oracle may reclaim *idle* free instances (a quiet Python bot is
light on CPU). Keep utilization visible by generating reports regularly, and
accept the small risk — if reclaimed, just recreate the instance. If you see
"out of host capacity" while creating the instance, retry later or pick
another availability domain.

1. Sign up at https://www.oracle.com/cloud/free/ (choose the **home region**
   wisely — the free VM must live there; Mumbai works from India).
2. In the Oracle console: hamburger menu → **Compute → Instances → Create**.
   - Name: watchdog
   - Image: **Canonical Ubuntu 22.04** (click "Edit" next to Image and Shape
     if needed; pick an "Always Free Eligible" image)
   - Shape: click Edit → **Ampere A1.Flex**, 1 OCPU + 2 GB RAM (plenty)
   - SSH key: "Generate a key pair" → download BOTH the private (.key) and
     public (.pub) files. Keep them safe.
   - Create.
3. Wait ~1 min, then click the instance → copy the **Public IP Address**.
4. Connect from your computer (Windows 10/11 and Mac have `ssh` built in):
   ```
   ssh -i <path-to-your-key-file> ubuntu@<public-ip>
   ```
   Accept the fingerprint question with `yes`. (File permission error on the
   key? On Mac/Linux run `chmod 400 <keyfile>`.)
5. On the server, install the basics:
   ```
   sudo apt update && sudo apt install -y python3-venv tmux
   ```
6. Upload the bot: on **your computer**, from the folder containing
   `presence-monitor`:
   ```
   scp -i <path-to-your-key-file> -r presence-monitor ubuntu@<public-ip>:~
   ```
   **Do NOT upload any `.session` or `.env` file containing credentials to a
   git repo — direct scp to your own server is fine.** (If you already logged
   in on your PC in Part 2, you can even upload `watchdog.session` and skip
   re-login on the server — it's your machine. Or just log in fresh there.)
7. On the server:
   ```
   cd ~/presence-monitor
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   cp .env.example .env
   nano .env          # fill in API_ID, API_HASH, GROUP, NTFY_TOPIC...
   ```
   (`nano` basics: edit normally, Ctrl+O then Enter to save, Ctrl+X to exit.)
8. Run it inside `tmux` so it survives your SSH window closing:
   ```
   tmux new -s watchdog
   venv/bin/python bot.py
   ```
   Log in when asked (phone + code). Once it prints "watchdog running",
   detach: press **Ctrl+B, then D**. Re-attach later any time with
   `tmux attach -t watchdog`.
9. Make it restart on server reboot: `crontab -e`, add at the end:
   ```
   @reboot sleep 60 && cd /home/ubuntu/presence-monitor && /home/ubuntu/presence-monitor/venv/bin/python bot.py >> watchdog.log 2>&1
   ```

### Option C — cheap VPS (₹99-500/month, most reliable)

Indian providers with UPI payment sell tiny Linux VPSs that are more than
enough. As of 2026 you can find 1 GB RAM KVM VPS for roughly ₹99-340/month
(e.g. HostStack from ~₹299, HeavenCloud from ~₹340; DigitalOcean's smallest
droplet is ~$6/₹504). Prices change — check what's current. For this bot,
even 512 MB RAM works.

The steps are identical to Oracle's Part B steps 4-9, except:
- the provider gives you the IP and usually a password instead of SSH keys,
  so `ssh ubuntu@<ip>` (or `root@<ip>`), and
- if asked, pick Ubuntu 22.04/24.04 as the OS.

---

## Part 4 — Daily use

- **Live alerts** arrive on ntfy and/or the log group automatically
  (floods, always-online users, mass deletions, joins, messages...).
- **Weekly review:** on the machine running the bot:
  ```
  venv/bin/python report.py -o report.md     # or: python report.py
  ```
  To get the report onto your PC from a server:
  ```
  scp -i <key> ubuntu@<public-ip>:~/presence-monitor/report.md .
  ```
- **The database** `presence.db` grows slowly (a few MB per month). Back it
  up the same way with scp if you care about the history.
- **Stopping:** in tmux, attach and press Ctrl+C. On your PC, close the
  terminal.

## Part 5 — Giving it to other moderators

Each mod runs their own instance on their own account: they do Part 1 with
their own Telegram account and their own my.telegram.org keys. You can point
everyone's `LOG_GROUP` at one shared log group and everyone's `NTFY_TOPIC`
at one shared topic, so the whole team sees the same feed. Never share
`.session` files or your `.env`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `API_ID or Hash cannot be empty` | `.env` missing or misnamed / values not filled; it must sit in the same folder as `bot.py` |
| `Could not find the input entity` for GROUP | Your account hasn't interacted with that group; open it once in the app, or use the invite link, or the numeric id |
| Login code not arriving | It's sent inside the Telegram app (service notifications), not SMS |
| `FloodWaitError` in logs | Normal protection — the bot waits it out automatically. If frequent, lower `TG_LOG_LEVEL` to `notable` |
| Nothing appears in log group | Check `LOG_GROUP` id (must be the `-100...` id for private groups); make sure your account is in that group |
| ntfy silent | Compare the topic spelling in `.env` and the app; test by opening `https://ntfy.sh/<topic>` in a browser |
| SQLite `database is locked` | You ran `report.py` while `bot.py` was writing — just run it again; reads are quick |
| Oracle says "out of host capacity" | Retry later, or choose another availability domain when creating the instance |
| Server rebooted, bot not running | Check step B.9's crontab entry; run `tmux attach` to see any startup error |

## Security recap

- `watchdog.session` = your account. Never share it, never put it in git.
- Keep 2FA on. If you ever suspect compromise: Telegram settings → Devices →
  terminate that session, and delete the session file.
- Add `.env`, `*.session`, `presence.db`, `watchdog.log` to `.gitignore`
  before ever putting this folder in a repository.
