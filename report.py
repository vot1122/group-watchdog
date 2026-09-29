#!/usr/bin/env python3
"""
Analyses presence.db produced by bot.py and prints a structured report:

  A. Spam / automation signals   - what you asked for, on top
  B. Not directly spam-related, but logged (might come in handy)

Usage:
    python report.py                      # print to console
    python report.py -o report.md         # also write markdown file
    python report.py --csv users.csv     # export flat metrics table
    python report.py --db other.db       # use a different database
"""

import argparse
import csv
import os
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def human(sec):
    if sec is None:
        return "n/a"
    sec = int(sec)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm%02ds" % (sec // 60, sec % 60)
    return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)


def _tz_list(db=None):
    """Active display zones: the /tz setting (if a db is given) > the
    TIMEZONES env var > Asia/Kolkata,UTC."""
    spec = None
    if db is not None:
        try:
            row = db.execute(
                "SELECT value FROM settings WHERE key='timezones'").fetchone()
            if row:
                spec = row[0]
        except sqlite3.OperationalError:
            pass
    spec = spec or os.environ.get("TIMEZONES", "Asia/Kolkata,UTC")
    objs = []
    for name in spec.split(","):
        try:
            objs.append(ZoneInfo(name.strip()))
        except Exception:
            pass
    return objs or [timezone.utc]


def fmt_tz(ts, tzs, with_date=False):
    """One timestamp rendered in every zone: '14:30 IST · 09:00 UTC'."""
    f = "%d %b %H:%M" if with_date else "%H:%M"
    return " · ".join(
        "%s %s" % (
            datetime.fromtimestamp(ts, tz=tz).strftime(f),
            datetime.fromtimestamp(ts, tz=tz).tzname() or str(tz))
        for tz in tzs)


def fmt_ts(ts):
    return fmt_tz(ts, _tz_list(), with_date=True)


# ------------------------------------------------------------- computation --

def compute(uid, rows):
    """rows: [(ts, online)] sorted by ts. Returns metric dict."""
    # Collapse consecutive duplicates -> keep only real transitions.
    tr = []
    for ts, on in rows:
        if tr and tr[-1][1] == on:
            continue
        tr.append((ts, on))

    now = time.time()
    first, last = tr[0][0], tr[-1][0]
    currently_online = tr[-1][1] == 1

    # If currently online, we have observed them from `first` until now.
    window_end = now if currently_online else last

    # If the user is currently online, close the session with 'now'.
    closed = tr + ([(now, 0)] if currently_online else [])

    sessions = []
    i = 0
    while i < len(closed) - 1:
        if closed[i][1] == 1 and closed[i + 1][1] == 0:
            sessions.append((closed[i][0], closed[i + 1][0]))
            i += 2
        else:
            i += 1

    gaps = [sessions[k + 1][0] - sessions[k][1] for k in range(len(sessions) - 1)]

    durs = [b - a for a, b in sessions]
    m = {
        "user_id": uid,
        "first": first,
        "last": window_end,
        "window_h": (window_end - first) / 3600,
        "n_trans": len(tr),
        "n_sessions": len(sessions),
        "uptime": sum(durs),
        "offline": (window_end - first) - sum(durs),  # observed offline time
        "avg_session": (sum(durs) / len(durs)) if durs else None,
        "max_session": max(durs, default=None),
        "gaps": gaps,
        "longest_gap": max(gaps, default=None),
        "gap_cv": (statistics.pstdev(gaps) / statistics.mean(gaps))
                  if len(gaps) >= 5 and statistics.mean(gaps) else None,
        "active_h": len({datetime.fromtimestamp(a, tz=timezone.utc).hour
                         for a, _ in sessions}),
        "online_events": sum(1 for _, on in tr if on),
        "first_online": tr[0][1] == 1,   # was already online when we started
    }
    m["uptime_ratio"] = (m["uptime"] / (window_end - first)) if window_end > first else None
    return m


def score(m, msg=None, churn=0):
    """Heuristic bot-likelihood score out of 100, with human-readable reasons."""
    reasons, s = [], 0
    msg = msg or {}

    # --- spam signals (message behaviour) ---
    if msg.get("peak") and msg["peak"] > 150:
        s += 20
        reasons.append("flood: %d msgs/hour" % msg["peak"])
    if msg.get("deleted", 0) >= 5 and msg.get("sent") and \
            msg["deleted"] / msg["sent"] >= 0.3:
        s += 10
        reasons.append("deleted %d/%d own msgs" % (msg["deleted"], msg["sent"]))
    if churn >= 3:
        s += 10
        reasons.append("join/leave churn x%d" % churn)

    # --- spam signals (presence behaviour) ---
    if m["window_h"] < 6:
        if not reasons:
            reasons.append("not enough data yet (needs ~a day)")
        return min(s, 100), reasons

    if m["uptime_ratio"] is not None and m["uptime_ratio"] >= 0.98 and m["window_h"] >= 24:
        # under 48h of observation a single no-sleep stint can look like
        # 24/7 - award the full weight only once two full days back it up
        s += 40 if m["window_h"] >= 48 else 24
        reasons.append("online 24/7")
    if m["gap_cv"] is not None and m["gap_cv"] < 0.15 and len(m["gaps"]) >= 10:
        s += 25
        reasons.append("machine-regular offline gaps")
    if m["active_h"] >= 20 and m["online_events"] >= 50:
        s += 20
        reasons.append("online in %d/24 hours, no sleep pattern" % m["active_h"])
    if m["longest_gap"] is not None and m["longest_gap"] < 3600 and m["window_h"] >= 48:
        s += 15
        reasons.append("never offline a full hour (2+ days)")
    if m["avg_session"] is not None and m["avg_session"] < 10 and m["n_sessions"] >= 20:
        s += 10
        reasons.append("micro-sessions, avg %s" % human(m["avg_session"]))

    if not reasons:
        reasons.append("no automation signals - looks human")
    return min(s, 100), reasons


def label(s):
    if s >= 70:
        return "VERY LIKELY AUTOMATED"
    if s >= 40:
        return "SUSPICIOUS"
    if s >= 15:
        return "MILDLY UNUSUAL"
    return "LOOKS HUMAN"


def message_stats(db):
    """per-user dict: sent / peak per hour / deleted / edits."""
    per = {}
    try:
        for uid, ts, deleted, edits in db.execute(
                "SELECT user_id, ts, deleted, edit_count FROM messages ORDER BY ts"):
            per.setdefault(uid, []).append((ts, deleted, edits))
    except sqlite3.OperationalError:
        return {}
    out = {}
    for uid, rows in per.items():
        tss = [t for t, _, _ in rows]
        peak = None
        if len(tss) >= 10:
            best, j = 0, 0
            for i in range(len(tss)):
                while tss[i] - tss[j] > 3600:
                    j += 1
                best = max(best, i - j + 1)
            peak = best
        out[uid] = {"sent": len(rows), "peak": peak,
                    "deleted": sum(1 for r in rows if r[1]),
                    "edits": sum(r[2] for r in rows)}
    return out


def churn_stats(db):
    """per-user join/leave counts + global event timeline."""
    per = {}
    try:
        for uid, kind in db.execute(
                "SELECT user_id, kind FROM group_events "
                "WHERE kind IN ('join','leave','kicked') AND user_id IS NOT NULL"):
            per.setdefault(uid, {"join": 0, "leave": 0, "kicked": 0})
            per[uid][kind] = per[uid].get(kind, 0) + 1
    except sqlite3.OperationalError:
        pass
    return {uid: min(v["join"], v["leave"] + v["kicked"]) for uid, v in per.items()}, per


# ----------------------------------------------------------------- output ---

# a member counts as "active" (tracked in report/boards) if they came
# online in the last ACTIVE_DAYS days; older ones are excluded until
# they return (survives restarts - computed from the data each time)
ACTIVE_DAYS = int(os.environ.get("ACTIVE_DAYS", "7"))


def _monitor_cutoff(db):
    """Events dated before the monitor started are Telegram-backfilled
    last-seen timestamps (sometimes months old), not observations -
    scoring must ignore them or window_h / uptime get skewed."""
    try:
        row = db.execute(
            "SELECT value FROM settings WHERE key='monitor_since'").fetchone()
        return float(row[0]) if row else 0.0
    except (sqlite3.OperationalError, TypeError, ValueError):
        return 0.0


def _observed_events(db):
    """All presence events, trimmed to the actual monitoring window and
    restricted to members who came online in the last ACTIVE_DAYS days -
    members not online since last week are excluded from everything."""
    events = {}
    for uid, ts, online in db.execute(
            "SELECT user_id, ts, online FROM presence_events ORDER BY ts"):
        events.setdefault(uid, []).append((ts, online))
    cutoff = _monitor_cutoff(db)
    if cutoff:
        events = {uid: [r for r in rows if r[0] >= cutoff]
                  for uid, rows in events.items()}
        events = {uid: rows for uid, rows in events.items() if rows}
    acut = time.time() - ACTIVE_DAYS * 86400
    active = {uid for uid, rows in events.items()
              if any(on and ts >= acut for ts, on in rows)}
    return {uid: rows for uid, rows in events.items() if uid in active}


def co_presence(events, min_trans=12, tol=20, ratio=0.8):
    """Accounts whose presence transitions repeatedly happen within seconds
    of each other - the signature of one operator (or clone tooling)
    running several accounts. Returns {uid: (partner, ratio_a, ratio_b)}.
    Conservative on purpose: both accounts need 12+ transitions and 80% of
    each account's transitions synced within a 20s window."""
    trans = {}
    for uid, rows in events.items():
        tr = []
        for ts, on in rows:
            if tr and tr[-1][1] == on:
                continue
            tr.append((ts, on))
        if len(tr) >= min_trans:
            trans[uid] = [t for t, _ in tr]

    def synced(x, y):
        c = k = 0
        for t in x:
            while k < len(y) and y[k] < t - tol:
                k += 1
            if k < len(y) and y[k] <= t + tol:
                c += 1
        return c

    out = {}
    uids = sorted(trans)
    for i in range(len(uids)):
        for j in range(i + 1, len(uids)):
            a, b = trans[uids[i]], trans[uids[j]]
            ca, cb = synced(a, b), synced(b, a)
            if ca >= ratio * len(a) and cb >= ratio * len(b):
                out[uids[i]] = (uids[j], ca / len(a), cb / len(b))
                out[uids[j]] = (uids[i], cb / len(b), ca / len(a))
    return out


def build_report(db):
    users = {r[0]: {"username": r[1], "display_name": r[2], "is_bot": r[3]}
             for r in db.execute(
                 "SELECT user_id, username, display_name, is_bot FROM users")}

    events = _observed_events(db)

    msg = message_stats(db)
    churn, _ = churn_stats(db)

    def uname(uid):
        u = users.get(uid, {})
        return u.get("username") or uid

    results = []
    for uid, rows in events.items():
        m = compute(uid, rows)
        m["msg"] = msg.get(uid)
        m["churn"] = churn.get(uid, 0)
        s, reasons = score(m, m["msg"], m["churn"])
        m["score"], m["reasons"] = s, reasons
        results.append(m)

    # co-presence: accounts that move in lockstep (one operator, many bots)
    pairs = co_presence(events)
    for m in results:
        p = pairs.get(m["user_id"])
        if p:
            m["score"] = min(100, m["score"] + 35)
            m["reasons"].append("moves in sync with %s (%d%% of transitions)"
                                % (uname(p[0]), round(100 * p[1])))
    # most uptime first (score as tie-breaker): the moderator wants to
    # eyeball the biggest uptime holders at the top; scores still mark
    # suspicious rows
    results.sort(key=lambda x: (-(x["uptime"] or 0), -x["score"]))

    lines = []
    a = lines.append

    now = time.time()
    first = _monitor_cutoff(db)
    a("# Group watchdog report")
    a("")
    a("Generated %s" % fmt_ts(now))
    if first:
        d = now - first
        a("")
        a("watching since %s (%dd%02dh) \u00b7 %d of %d members online in "
          "the last %d days" % (
              fmt_ts(first), d // 86400, (d % 86400) // 3600,
              len(results), len(users), ACTIVE_DAYS))
    else:
        a("")
        a("%d of %d members online in the last %d days"
          % (len(results), len(users), ACTIVE_DAYS))
    a("")

    # --------------------------- the one table ----------------------------
    a("## Member activity - last %d days (most uptime first)" % ACTIVE_DAYS)
    a("")
    a("| user | last seen | uptime | sessions | avg session | "
      "longest offline | msgs | deleted | score | signals |")
    a("|---|---|---|---|---|---|---|---|---|---|")
    for m in results:
        ms = m["msg"] or {}
        ratio = (("%.0f%%" % (100 * m["uptime_ratio"]))
                 if m["uptime_ratio"] is not None else "-")
        sig = "; ".join(m["reasons"][:2]) if m["reasons"] else "-"
        a("| %s | %s | %s (%s) | %d | %s | %s | %s | %s | %d | %s |" % (
            uname(m["user_id"]), fmt_ts(m["last"]),
            human(m["uptime"]), ratio, m["n_sessions"],
            human(m["avg_session"]), human(m["longest_gap"]),
            ms.get("sent", "-"), ms.get("deleted", "-"),
            m["score"], sig))
    a("")

    # ----------------------- coordinated accounts -------------------------
    if pairs:
        a("## Coordinated accounts (presence moves together)")
        a("")
        a("These accounts go online/offline within seconds of each other on"
          " most of their transitions - a strong sign of one operator or"
          " clone tooling. Verify manually before acting.")
        a("")
        seen_pairs = set()
        for uid, (other, ra, rb) in pairs.items():
            key = tuple(sorted((uid, other)))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            a("- **%s** and **%s** - %d%% / %d%% of their transitions synced"
              % (uname(uid), uname(other), round(100 * ra), round(100 * rb)))
        a("")

    # ----------------------- recent group activity ------------------------
    jlt = list(db.execute(
        "SELECT ts, user_id, kind, data FROM group_events "
        "WHERE kind IN ('join','leave','kicked') ORDER BY ts DESC LIMIT 10"))
    changes = list(db.execute(
        "SELECT ts, user_id, kind, data FROM group_events "
        "WHERE kind IN ('username_change','name_change') "
        "ORDER BY ts DESC LIMIT 10"))
    if jlt or changes:
        a("## Recent group activity (newest first, last 10)")
        a("")
        for ts, uid, kind, data in jlt:
            a("- %s - %s %s" % (fmt_ts(ts), uname(uid), data))
        for ts, uid, kind, data in changes:
            a("- %s - %s changed %s to %s" % (
                fmt_ts(ts), uname(uid),
                "username" if kind == "username_change" else "name", data))
        a("")

    # --------------------------- how to read this -------------------------
    a("## How to read this")
    a("")
    a("- score: 0-39 looks human, 40-69 suspicious, 70+ likely automation."
      " Heuristics, not proof - always review manually before acting.")
    a("- uptime = total time online while monitored (share of the observed"
      " window in brackets); signals lists the main scoring reasons.")
    a("- only members who came online in the last %d days are listed here."
      " Members who hide their 'last seen' cannot be tracked by anyone."
      % ACTIVE_DAYS)
    return "\n".join(lines), results


def _knum(n):
    if n >= 1000:
        s = "%.1fk" % (n / 1000)
        return s.rstrip("0").rstrip(".")
    return str(n)


def compact_report(db, limit=6):
    """Brief live dashboard shown in the log group's LIVE REPORT message."""
    users = {r[0]: {"username": r[1], "display_name": r[2]}
             for r in db.execute(
                 "SELECT user_id, username, display_name, is_bot FROM users")}
    events = _observed_events(db)
    msg = message_stats(db)
    churn, _ = churn_stats(db)
    results = []
    for uid, rows in events.items():
        m = compute(uid, rows)
        m["msg"] = msg.get(uid)
        m["churn"] = churn.get(uid, 0)
        s, reasons = score(m, m["msg"], m["churn"])
        m["score"], m["reasons"] = s, reasons
        results.append(m)
    # most uptime first, score as tie-breaker - matches the full report
    results.sort(key=lambda x: (-(x["uptime"] or 0), -x["score"]))

    def uname(uid):
        u = users.get(uid, {})
        return u.get("username") or uid

    now = datetime.now(timezone.utc)
    # "watching" = when monitoring actually began (persisted by bot.py as a
    # setting), NOT the oldest event - Telegram backfills real last-seen
    # timestamps that can be months in the past.
    first = None
    try:
        row = db.execute(
            "SELECT value FROM settings WHERE key='monitor_since'").fetchone()
        if row:
            first = float(row[0])
    except (sqlite3.OperationalError, TypeError, ValueError):
        pass
    if first is None:
        first = min((rows[0][0] for rows in events.values()), default=None)
    watching = ""
    if first:
        d = now.timestamp() - first
        watching = " · watching %dd%02dh" % (d // 86400, (d % 86400) // 3600)

    lines = ["\U0001F4CA LIVE REPORT · %s%s" % (
        fmt_tz(now.timestamp(), _tz_list(db), with_date=True), watching)]

    shown = results[:limit]      # already sorted: most uptime first
    if shown:
        lines.append("─ top uptime ─")
        for m in shown:
            mark = ("\U0001F534" if m["score"] >= 70
                    else "\U0001F7E1" if m["score"] >= 40 else "\u25CB")
            line = "%s %s %s · %d" % (
                mark, uname(m["user_id"]), human(m["uptime"]), m["score"])
            if m["score"] > 0 and m["reasons"]:
                line += " · " + m["reasons"][0][:50]
            lines.append(line)
    else:
        lines.append("no presence data yet")

    try:
        n_msgs = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        n_del = db.execute(
            "SELECT COUNT(*) FROM messages WHERE deleted=1").fetchone()[0]
    except sqlite3.OperationalError:
        n_msgs = n_del = 0
    n_ev = sum(len(v) for v in events.values())
    lines.append("─ stats ─")
    lines.append("%d members · %s events · %s msgs (%s del)" % (
        len(users), _knum(n_ev), _knum(n_msgs), _knum(n_del)))
    talkers = sorted(((m["msg"]["sent"], m) for m in results if m["msg"]),
                      key=lambda x: -x[0])[:5]
    if talkers:
        lines.append("talkers: " + " · ".join(
            "%s %s" % (uname(m["user_id"]), sent) for sent, m in talkers))
    return "\n".join(lines)[:3800]


def export_csv(results, path, users):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["user_id", "username", "display_name", "score", "verdict",
                    "window_hours", "sessions", "uptime_hours", "offline_hours",
                    "uptime_ratio", "avg_session_s", "max_session_s",
                    "longest_gap_s", "active_hours_of_24",
                    "messages", "deleted", "edits", "join_leave_cycles"])
        for m in results:
            u = users.get(m["user_id"], {})
            ms = m["msg"] or {}
            w.writerow([m["user_id"], u.get("username"), u.get("display_name"),
                        m["score"], label(m["score"]),
                        "%.2f" % m["window_h"], m["n_sessions"],
                        "%.2f" % (m["uptime"] / 3600),
                        "%.2f" % (m["offline"] / 3600),
                        "%.3f" % m["uptime_ratio"] if m["uptime_ratio"] is not None else "",
                        "%.1f" % m["avg_session"] if m["avg_session"] is not None else "",
                        "%.1f" % m["max_session"] if m["max_session"] is not None else "",
                        "%.1f" % m["longest_gap"] if m["longest_gap"] is not None else "",
                        m["active_h"],
                        ms.get("sent", 0), ms.get("deleted", 0),
                        ms.get("edits", 0), m["churn"]])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="presence.db")
    ap.add_argument("-o", "--output", help="write markdown report to this file")
    ap.add_argument("--csv", help="export flat metrics to this CSV file")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    report, results = build_report(db)
    print(report)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print("\n[written to %s]" % args.output)
    if args.csv:
        users = {r[0]: {"username": r[1], "display_name": r[2]}
                 for r in db.execute("SELECT user_id, username, display_name, is_bot FROM users")}
        export_csv(results, args.csv, users)
        print("[csv written to %s]" % args.csv)


if __name__ == "__main__":
    main()
