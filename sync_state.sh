#!/usr/bin/env bash
# Pushes the heartbeat (and periodically the encrypted database) to the
# `state` branch, so the next GitHub Actions runner can take over.
#
# Usage: sync_state.sh [full]     ("full" = always include the database)
#
# Requires env: DB_PASS (encryption passphrase), GITHUB_TOKEN, GITHUB_REPOSITORY
# (the last two are provided automatically inside GitHub Actions).

set -u
cd "$(dirname "$0")"

if [ -z "${GITHUB_TOKEN:-}" ]; then
    echo "sync_state.sh: GITHUB_TOKEN is not set - cannot push state" >&2
    exit 1
fi

MODE=${1:-}

# Heartbeat every call; the database every 8th call (~every 32 min) or when
# forced - keeps the amount of data pushed per hour reasonable.
COUNT_FILE=/tmp/sync_count
COUNT=$(( $(cat "$COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$COUNT" > "$COUNT_FILE"

INCLUDE_DB=0
[ "${MODE}" = "full" ] && INCLUDE_DB=1
[ $((COUNT % 8)) -eq 0 ] && INCLUDE_DB=1

STATE_DIR=$(mktemp -d)
trap 'rm -rf "$STATE_DIR" /tmp/state-snap.db 2>/dev/null' EXIT

date +%s > "$STATE_DIR/heartbeat"

if [ "$INCLUDE_DB" = "1" ] && [ -f presence.db ]; then
    # Safe snapshot while the bot is writing (SQLite online backup).
    # 310k PBKDF2 iterations (OWASP recommendation; the openssl default
    # of 10k is weak for short passphrases).
    sqlite3 presence.db ".backup /tmp/state-snap.db"
    gzip -c /tmp/state-snap.db | openssl enc -aes-256-cbc -pbkdf2 -iter 310000 -salt \
        -out "$STATE_DIR/presence.db.enc" -pass env:DB_PASS
fi

cd "$STATE_DIR"
git init -q
git config user.name "watchdog-bot"
git config user.email "watchdog-bot@users.noreply.github.com"
git add -A
git commit -qm "state update"
# Single-commit branch (force push) - history never grows.
git push -q --force \
    "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" \
    HEAD:state
