#!/bin/bash
set -eo pipefail

cd /Users/helm/Projects/kat-farmer || exit 1
export PATH="/Users/helm/.nvm/versions/node/v22.22.0/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Source API keys for wrangler (CLOUDFLARE_API_TOKEN) — launchd does not inherit shell env
[ -f /Users/helm/.api-keys ] && source /Users/helm/.api-keys

PYTHON="/Users/helm/.claude/venv/bin/python3"

# Supply runs FIRST — indexer reads supply_data.json for circSupply
if ! $PYTHON supply.py --json 2>&1; then
    echo "supply.py failed, continuing with stale supply_data.json" >&2
fi

# Indexer — if this fails, skip deploy entirely
$PYTHON indexer.py

# Only commit data files (never state files)
git add data.json supply_data.json snapshots.json

CHANGES=$(git diff --cached --stat)
if [ -z "$CHANGES" ]; then
    echo "No data changes, skipping commit"
    exit 0
fi

git commit -m "auto: $(date +%Y-%m-%d\ %H:%M)"
git push origin main 2>&1 || echo "Push failed, will retry next run" >&2

# Deploy to Cloudflare Pages (direct upload — git push does NOT trigger auto-deploy)
npx wrangler pages deploy . --project-name=kat-stats --branch=main --commit-dirty=true 2>&1 || echo "CF Pages deploy failed, will retry next run" >&2
