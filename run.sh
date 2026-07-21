#!/bin/bash
set -eo pipefail

cd /Users/helm/Projects/kat-farmer || exit 1
export PATH="/Users/helm/.nvm/versions/node/v22.22.0/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Source API keys for wrangler (CLOUDFLARE_API_TOKEN) — launchd does not inherit shell env
[ -f /Users/helm/.api-keys ] && source /Users/helm/.api-keys

PYTHON="/Users/helm/.claude/venv/bin/python3"

# Stay in sync with origin BEFORE doing anything. Code (index.html/app.js/*.py)
# may be updated from another machine; this box only WRITES data. Without this,
# a push from elsewhere leaves us behind → our push is rejected → we redeploy
# our STALE working dir every hour, reverting the live site. Adopt origin first.
git fetch origin main 2>&1 || echo "git fetch failed, proceeding with local" >&2
git reset --hard origin/main 2>&1 || echo "git reset failed, proceeding with local" >&2

# Loudly flag code drift: if we couldn't adopt origin (e.g. git creds unavailable
# under launchd), we're about to regenerate data on STALE code and deploy it.
# This is exactly the failure that froze the box on old code for 11 days.
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
    echo "WARN: HEAD != origin/main after self-heal — box is on STALE CODE (check git credentials under launchd)" >&2
fi

# Supply runs FIRST — indexer reads supply_data.json for circSupply
if ! $PYTHON supply.py --json 2>&1; then
    echo "supply.py failed, continuing with stale supply_data.json" >&2
fi

# Indexer — if this fails, skip deploy entirely
$PYTHON indexer.py

# Holders vs Users tab — heavy (~10 min full pull), so refresh only ~daily
HA="holder_activity.json"
HA_STALE=1
if [ -f "$HA" ]; then
    AGE=$(( $(date +%s) - $(stat -f %m "$HA") ))
    [ "$AGE" -lt 72000 ] && HA_STALE=0   # < 20h old → keep
fi
if [ "$HA_STALE" -eq 1 ]; then
    $PYTHON holder_activity.py --refresh 2>&1 || echo "holder_activity.py failed, keeping stale $HA" >&2
fi

# ── Data-integrity gate ──────────────────────────────────────────────────────
# Never commit or deploy broken/missing data. holder_activity.py now writes
# atomically, but a refresh can still legitimately fail — in that case keep the
# last-good copy rather than shipping a missing file (which serves the SPA
# fallback and breaks the Holders-vs-Users tab).
valid_json() { "$PYTHON" -c "import json,sys; sys.exit(0 if json.load(open(sys.argv[1])) else 1)" "$1" 2>/dev/null; }

if valid_json "$HA"; then
    cp -f "$HA" "$HA.last-good"
else
    echo "WARN: $HA missing/invalid after refresh" >&2
    [ -f "$HA.last-good" ] && cp -f "$HA.last-good" "$HA" && echo "restored $HA from .last-good" >&2
fi

for f in data.json supply_data.json snapshots.json holder_activity.json; do
    if ! valid_json "$f"; then
        echo "FATAL: $f is not valid JSON — aborting before commit/deploy (refusing to publish broken data)" >&2
        exit 1
    fi
done

# Only commit data files (never state files)
git add data.json supply_data.json snapshots.json holder_activity.json

CHANGES=$(git diff --cached --stat)
if [ -z "$CHANGES" ]; then
    echo "No data changes, skipping commit"
    exit 0
fi

git commit -m "auto: $(date +%Y-%m-%d\ %H:%M)"
git push origin main 2>&1 || echo "Push failed, will retry next run" >&2

# Deploy to Cloudflare Pages (direct upload — git push does NOT trigger auto-deploy)
npx wrangler pages deploy . --project-name=kat-stats --branch=main --commit-dirty=true 2>&1 || echo "CF Pages deploy failed, will retry next run" >&2
