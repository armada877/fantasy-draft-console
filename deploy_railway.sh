#!/usr/bin/env bash
# Deploy the draft console to Railway.
#
# WHY A STAGED BUNDLE: this repo has a PUBLIC GitHub remote, so the console payload
# (draft_app/static/index.html, data.json) and config/briefing.md are gitignored — a
# git-based deploy would ship an empty console with a generic advisor. So we stage
# exactly the runtime files into a clean directory and `railway up` from there. That
# also guarantees ESPN cookies, raw scrapes, the .xlsm and analysis/ never leave the box.
#
# Usage:
#   ./deploy_railway.sh --dry-run     # stage + list the bundle, deploy nothing
#   ./deploy_railway.sh               # stage, then `railway up`
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

APP="$ROOT/draft_app"
[[ -f "$APP/static/index.html" ]] || {
  echo "✗ draft_app/static/index.html missing — run: python3 pipeline.py build inject" >&2; exit 1; }

BUNDLE="$(mktemp -d -t draft-console-deploy)"
trap 'rm -rf "$BUNDLE"' EXIT

# ── the runtime surface, and nothing else ──
mkdir -p "$BUNDLE/static" "$BUNDLE/config"
cp "$APP/server.py" "$APP/requirements.txt" "$APP/Procfile" "$APP/railway.json" "$BUNDLE/"
cp "$APP/static/index.html" "$BUNDLE/static/"
[[ -f "$APP/static/data.json" ]] && cp "$APP/static/data.json" "$BUNDLE/static/"
if [[ -f "$ROOT/config/briefing.md" ]]; then
  cp "$ROOT/config/briefing.md" "$BUNDLE/config/"     # advisor system prompt
else
  echo "⚠ config/briefing.md absent — advisor falls back to the generic briefing."
fi

# ── refuse to ship anything secret, even if someone adds a cp above ──
LEAKS="$(cd "$BUNDLE" && find . -type f \
  \( -name '.env' -o -name '*.espn_auth*' -o -name '*.xlsm' -o -name '*.har' \
     -o -name 'league.json' -o -name 'tendencies.json' -o -name '*.pem' \) )"
if [[ -n "$LEAKS" ]]; then echo "✗ refusing to deploy, secret-ish files staged:"; echo "$LEAKS"; exit 1; fi
if (cd "$BUNDLE" && grep -rlI --exclude-dir=static -e 'sk-ant-' -e 'espn_s2' . 2>/dev/null | grep -q .); then
  echo "✗ refusing to deploy: a staged file contains a credential-shaped string" >&2; exit 1
fi

echo "── deploy bundle ($(du -sh "$BUNDLE" | cut -f1)) ──"
(cd "$BUNDLE" && find . -type f | sort | sed 's/^\./  /')
echo
echo "Runtime env vars the service needs:"
echo "  ANTHROPIC_API_KEY        (advisor; without it /api/advise returns 503)"
echo "  CONSOLE_PASSWORD         (HTTP Basic gate; without it the URL is PUBLIC)"
echo "  STRATEGY_BRIEFING_PATH=/app/config/briefing.md"
echo

if [[ $DRY -eq 1 ]]; then echo "(dry run — nothing deployed)"; exit 0; fi
railway whoami >/dev/null 2>&1 || { echo "✗ not logged in. Run: railway login" >&2; exit 1; }

# The bundle is a temp dir, so it is NOT the linked directory — target the project and
# service explicitly (read from this repo's link) instead of relying on cwd linkage.
PROJECT_ID="${RAILWAY_PROJECT_ID:-$(cd "$ROOT" && railway status --json 2>/dev/null \
  | "${PY:-python3}" -c 'import json,sys; print(json.load(sys.stdin).get("id",""))' 2>/dev/null)}"
SERVICE="${RAILWAY_SERVICE:-console}"
ENVIRONMENT="${RAILWAY_ENVIRONMENT:-production}"   # --project requires --environment
[[ -n "$PROJECT_ID" ]] || { echo "✗ could not resolve the linked project — run: railway init" >&2; exit 1; }

echo "→ railway up  (project $PROJECT_ID, service $SERVICE, env $ENVIRONMENT)"
cd "$BUNDLE" && railway up --detach \
  --service "$SERVICE" --project "$PROJECT_ID" --environment "$ENVIRONMENT"
