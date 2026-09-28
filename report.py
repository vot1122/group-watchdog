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
import sqlite3
import statistics
import time
from datetime import datetime, timezone


def human(sec):
    if sec is None:
        return "n/a"
    sec = int(sec)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm%02ds" % (sec // 60, sec % 60)
    return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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
    if msg.get("peak") and msg["peak"] > 100:
        s += 20
        reasons.append("message flood: %d messages in a single hour" % msg["peak"])
    if msg.get("deleted", 0) >= 5 and msg.get("sent") and \
            msg["deleted"] / msg["sent"] >= 0.3:
        s += 10
        reasons.append("deleted %d of their own %d messages" % (
            msg["deleted"], msg["sent"]))
    if churn >= 3:
        s += 10
        reasons.append("joined/left the group %d times (churn)" % churn)

    # --- spam signals (presence behaviour) ---
    if m["window_h"] < 6:
        if not reasons:
            reasons.append("not enough data yet - keep the monitor running "
                           "for at least a day")
        return min(s, 100), reasons

    if m["uptime_ratio"] is not None and m["uptime_ratio"] >= 0.98 and m["window_h"] >= 24:
        s += 40
        reasons.append("online ~24/7 across a full day+ (real humans sleep; "
                       "userbots and automation scripts stay connected)")
    if m["gap_cv"] is not None and m["gap_cv"] < 0.15 and len(m["gaps"]) >= 10:
        s += 25
        reasons.append("offline gaps are machine-regular "
                       "(coefficient of variation %.2f)" % m["gap_cv"])
    if m["active_h"] >= 20 and m["online_events"] >= 50:
        s += 20
        reasons.append("comes online in %d of the 24 hours of the day "
                       "- no sleep pattern" % m["active_h"])
    if m["longest_gap"] is not None and m["longest_gap"] < 3600 and m["window_h"] >= 48:
        s += 15
        reasons.append("never offline for a full hour across 2+ days")
    if m["avg_session"] is not None and m["avg_session"] < 10 and m["n_sessions"] >= 20:
        s += 10
        reasons.append("many very short sessions (avg %s) - typical of scripted "
                       "pings" % human(m["avg_session"]))

    if not reasons:
        reasons.append("no automation signals found - presence pattern looks human")
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

def build_report(db):
    users = {r[0]: {"username": r[1], "display_name": r[2], "is_bot": r[3]}
             for r in db.execute(
                 "SELECT user_id, username, display_name, is_bot FROM users")}

    events = {}
    for uid, ts, online in db.execute(
            "SELECT user_id, ts, online FROM presence_events ORDER BY ts"):
        events.setdefault(uid, []).append((ts, online))

    msg = message_stats(db)
    churn, joins_leaves = churn_stats(db)
    known_bots = {uid for uid, u in users.items() if u["is_bot"]}
    no_data = [uid for uid in users if uid not in events]

    results = []
    for uid, rows in events.items():
        m = compute(uid, rows)
        m["msg"] = msg.get(uid)
        m["churn"] = churn.get(uid, 0)
        s, reasons = score(m, m["msg"], m["churn"])
        m["score"], m["reasons"] = s, reasons
        results.append(m)
    results.sort(key=lambda x: -x["score"])

    def uname(uid):
        u = users.get(uid, {})
        return u.get("username") or uid

    lines = []
    a = lines.append

    a("# Group watchdog report")
    a("")
    a("Generated %s (all times UTC)" % fmt_ts(time.time()))
    a("")

    # ============================ SECTION A ================================
    a("## A. Spam / automation signals")
    a("")

    a("### Presence + behaviour scores (most suspicious first)")
    a("")
    a("| user | score | verdict | observed | sessions | uptime | avg session | "
      "longest offline | active hrs/24 | msgs | deleted |")
    a("|---|---|---|---|---|---|---|---|---|---|---|")
    for m in results:
        uptime = ("%.0f%%" % (100 * m["uptime_ratio"])) if m["uptime_ratio"] is not None else "n/a"
        ms = m["msg"] or {}
        a("| %s | %d | %s | %.1fh | %d | %s | %s | %s | %d | %s | %s |" % (
            uname(m["user_id"]), m["score"], label(m["score"]), m["window_h"],
            m["n_sessions"], uptime, human(m["avg_session"]),
            human(m["longest_gap"]), m["active_h"],
            ms.get("sent", "-"), ms.get("deleted", "-")))
    a("")

    heavy_deleters = [(m["msg"]["deleted"], m["msg"]["sent"], m)
                      for m in results if m["msg"] and m["msg"]["deleted"] >= 3]
    if heavy_deleters:
        a("### Deletion-heavy accounts (post-then-delete is a spam pattern)")
        a("")
        for deleted, sent, m in sorted(heavy_deleters, reverse=True):
            a("- %s deleted **%d of %d** messages they sent" % (
                uname(m["user_id"]), deleted, sent))
        a("")

    churners = [(m["churn"], m) for m in results if m["churn"] >= 2]
    if churners:
        a("### Join/leave churn")
        a("")
        for c, m in sorted(churners, reverse=True):
            a("- %s joined/left the group %d times" % (uname(m["user_id"]), c))
        a("")

    if known_bots:
        a("### Registered Telegram bots detected")
        a("")
        for uid in known_bots:
            u = users[uid]
            a("- %s%s" % (u["username"] or uid,
                          (" (%s)" % u["display_name"]) if u["display_name"] else ""))
        a("")

    a("### Per-user signal details")
    a("")
    for m in results:
        u = users.get(m["user_id"], {})
        name = u.get("username") or m["user_id"]
        a("#### %s%s" % (name, (" (%s)" % u["display_name"]) if u.get("display_name") else ""))
        a("")
        a("- verdict: **%s** (score %d/100)" % (label(m["score"]), m["score"]))
        for r in m["reasons"]:
            a("- %s" % r)
        a("")

    # ============================ SECTION B ================================
    a("## B. Not directly spam-related, but logged (might come in handy)")
    a("")

    total_msgs = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    total_deleted = db.execute(
        "SELECT COUNT(*) FROM messages WHERE deleted=1").fetchone()[0]
    total_edits = db.execute(
        "SELECT COALESCE(SUM(edit_count),0) FROM messages").fetchone()[0]
    total_presence = db.execute(
        "SELECT COUNT(*) FROM presence_events").fetchone()[0]
    ge_counts = dict(db.execute(
        "SELECT kind, COUNT(*) FROM group_events GROUP BY kind").fetchall())
    a("### Everything recorded so far")
    a("")
    a("- presence events logged: %d" % total_presence)
    a("- messages logged: %d (%d later edited, %d later deleted)"
      % (total_msgs, total_edits, total_deleted))
    a("- group events: %s" % (
        ", ".join("%d %s" % (v, k) for k, v in sorted(ge_counts.items()))
        or "none"))
    a("")

    talkers = sorted((m["msg"]["sent"], m) for m in results if m["msg"])
    if talkers:
        a("### Top talkers (messages sent while monitored)")
        a("")
        for sent, m in sorted(talkers, key=lambda x: -x[0])[:10]:
            a("- %s: %d messages (%d edited, %d deleted)" % (
                uname(m["user_id"]), sent,
                m["msg"]["edits"], m["msg"]["deleted"]))
        a("")

    changes = list(db.execute(
        "SELECT ts, user_id, kind, data FROM group_events "
        "WHERE kind IN ('username_change','name_change') ORDER BY ts DESC LIMIT 50"))
    if changes:
        a("### Username / name changes (most recent first)")
        a("")
        for ts, uid, kind, data in changes:
            a("- %s - %s %s: %s" % (fmt_ts(ts), uname(uid),
                                     "username" if kind == "username_change" else "name",
                                     data))
        a("")

    jlt = list(db.execute(
        "SELECT ts, user_id, kind, data FROM group_events "
        "WHERE kind IN ('join','leave','kicked') ORDER BY ts DESC LIMIT 30"))
    if jlt:
        a("### Recent joins / leaves / kicks")
        a("")
        for ts, uid, kind, data in jlt:
            a("- %s - %s %s" % (fmt_ts(ts), uname(uid), data))
        a("")

    if no_data:
        a("### Members with no presence data")
        a("")
        a("(Last seen hidden by privacy settings - cannot be tracked by anyone, "
          "or never seen online while monitoring.)")
        a("")
        for uid in no_data:
            u = users[uid]
            a("- %s%s" % (u["username"] or uid,
                          (" (%s)" % u["display_name"]) if u["display_name"] else ""))
        a("")

    # ============================ details =================================
    a("## Full presence details per user")
    a("")
    for m in results:
        u = users.get(m["user_id"], {})
        name = u.get("username") or m["user_id"]
        a("### %s%s" % (name, (" (%s)" % u["display_name"]) if u.get("display_name") else ""))
        a("")
        a("- observed: %s -> %s (%.1f hours)" % (
            fmt_ts(m["first"]), fmt_ts(m["last"]), m["window_h"]))
        if m.get("first_online"):
            a("- note: was already online when monitoring began - uptime is "
              "counted from when we first saw them")
        a("- total uptime: %s across %d sessions" % (human(m["uptime"]), m["n_sessions"]))
        a("- total offline time (observed): %s" % human(m["offline"]))
        if m["uptime_ratio"] is not None:
            a("- uptime ratio: %.1f%%" % (100 * m["uptime_ratio"]))
        a("- longest session: %s, average session: %s" % (
            human(m["max_session"]), human(m["avg_session"])))
        a("- longest offline gap: %s" % human(m["longest_gap"]))
        a("")

    a("### Notes")
    a("")
    a("- Members who hide their 'last seen' privacy cannot be tracked by anyone, "
      "including this tool.")
    a("- Scores are heuristics, not proof. Always combine with manual review "
      "(message content, account age, join date) before acting.")
    return "\n".join(lines), results


def compact_report(db, limit=8):
    """Short live summary shown in the log group's LIVE REPORT message."""
    users = {r[0]: {"username": r[1], "display_name": r[2]}
             for r in db.execute(
                 "SELECT user_id, username, display_name, is_bot FROM users")}
    events = {}
    for uid, ts, online in db.execute(
            "SELECT user_id, ts, online FROM presence_events ORDER BY ts"):
        events.setdefault(uid, []).append((ts, online))
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
    results.sort(key=lambda x: -x["score"])

    def uname(uid):
        u = users.get(uid, {})
        return u.get("username") or uid

    lines = ["LIVE REPORT - updated %s UTC" %
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), ""]

    shown = [m for m in results if m["score"] > 0][:limit]
    if shown:
        lines.append("Top suspects:")
        for i, m in enumerate(shown, 1):
            lines.append("%d. %s - %d/100 %s - %s" % (
                i, uname(m["user_id"]), m["score"], label(m["score"]),
                (m["reasons"][0] if m["reasons"] else "")[:70]))
    else:
        lines.append("No automation signals yet (needs ~a day of data).")
    lines.append("")
    try:
        n_msgs = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        n_del = db.execute(
            "SELECT COUNT(*) FROM messages WHERE deleted=1").fetchone()[0]
    except sqlite3.OperationalError:
        n_msgs = n_del = 0
    n_ev = sum(len(v) for v in events.values())
    lines.append("members: %d | presence events: %d | messages: %d (%d deleted)"
                 % (len(users), n_ev, n_msgs, n_del))
    talkers = sorted(((m["msg"]["sent"], m) for m in results if m["msg"]),
                      key=lambda x: -x[0])[:5]
    if talkers:
        lines.append("")
        lines.append("Top talkers: " + ", ".join(
            "%s (%d)" % (uname(m["user_id"]), sent) for sent, m in talkers))
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
