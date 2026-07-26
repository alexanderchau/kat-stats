#!/bin/bash
# Kick the pipeline when the data has gone stale.
#
# Why this exists: on 2026-07-26 the site served a 26h-old snapshot. The box had
# stopped running the hourly job (it was asleep from ~Jul-25 09:26 to Jul-26
# 11:05) and NOTHING noticed — the only watchdog was a scheduled task on a
# laptop, whose /api/trigger call was itself broken. StartInterval jobs do not
# make up missed firings, so after a gap the box waits up to a full hour more.
#
# This runs every 30 min and forces a run if data.json is older than MAX_AGE_H,
# bounding recovery-after-gap to ~30 min instead of "until a human looks".
set -uo pipefail

cd /Users/helm/Projects/kat-farmer || exit 1
export PATH="/Users/helm/.nvm/versions/node/v22.22.0/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

PYTHON="/Users/helm/.claude/venv/bin/python3"
MAX_AGE_H=3
LABEL="gui/$(id -u)/com.katana.kat-stats"
STAMP="[$(date '+%Y-%m-%d %H:%M')]"

# Never kick while a run is in flight — that is exactly the race run.sh's lock
# exists to prevent, and a long holder_activity.py refresh is not a stall.
LOCK_PID=$(cat /Users/helm/Projects/kat-farmer/.run.lock/pid 2>/dev/null || echo "")
if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
    echo "$STAMP pipeline already running (pid $LOCK_PID) — nothing to do"
    exit 0
fi

AGE=$("$PYTHON" - <<'PY' 2>/dev/null
import datetime, json, sys
try:
    meta = json.load(open("/Users/helm/Projects/kat-farmer/data.json"))["meta"]
    t = datetime.datetime.fromisoformat(meta["generatedAt"])
    now = datetime.datetime.now(datetime.timezone.utc)
    print(f'{(now - t).total_seconds() / 3600:.2f}')
except Exception as e:
    print("ERR", e, file=sys.stderr)
    sys.exit(1)
PY
)

if [ -z "$AGE" ]; then
    # Unreadable data.json is itself a reason to run, not a reason to stay quiet.
    echo "$STAMP WARN: could not read data.json generatedAt — kicking pipeline" >&2
    launchctl kickstart -k "$LABEL" && echo "$STAMP kicked $LABEL"
    exit 0
fi

if [ "$(echo "$AGE > $MAX_AGE_H" | bc -l)" -eq 1 ]; then
    echo "$STAMP STALE: data is ${AGE}h old (> ${MAX_AGE_H}h) — kicking $LABEL" >&2
    launchctl kickstart -k "$LABEL" && echo "$STAMP kicked $LABEL"
else
    echo "$STAMP ok: data is ${AGE}h old"
fi
