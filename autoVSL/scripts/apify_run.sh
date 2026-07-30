#!/usr/bin/env bash
# Run an Apify actor synchronously and save its dataset items.
# Implements the run-sync-get-dataset-items pattern from
# .claude/skills/prospector/references/apify-playbook.md
#
# Usage:
#   scripts/apify_run.sh <ACTOR_ID> <input.json> [output.json]
#   echo '{"searchQueries":["..."]}' | scripts/apify_run.sh <ACTOR_ID> - [output.json]
#
# ACTOR_ID format: username~actor-name (e.g. curious_coder~facebook-ads-library-scraper)
# Requires APIFY_TOKEN in the environment. Never hardcode or commit the token.

set -euo pipefail

# pwd -W gives a Windows-style path under Git Bash (needed by Windows Python)
REPO_ROOT="$(cd "$(dirname "$0")/.." && (pwd -W 2>/dev/null || pwd))"
export PYTHONUTF8=1

# Token: env var wins; fall back to the project's gitignored .env
if [[ -z "${APIFY_TOKEN:-}" && -f "${REPO_ROOT}/.env" ]]; then
  APIFY_TOKEN="$(grep -E '^APIFY_TOKEN=' "${REPO_ROOT}/.env" | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
fi

if [[ -z "${APIFY_TOKEN:-}" ]]; then
  echo "ERROR: APIFY_TOKEN is not set. Add APIFY_TOKEN=... to .env or export it (never commit it)." >&2
  exit 1
fi

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <ACTOR_ID> <input.json | -> [output.json]" >&2
  exit 1
fi

ACTOR_ID="$1"
INPUT="$2"
STAMP="$(date +%Y-%m-%d-%H%M%S)"
DEFAULT_OUT="${REPO_ROOT}/research/raw/${ACTOR_ID//[^a-zA-Z0-9_-]/_}-${STAMP}.json"
OUT="${3:-$DEFAULT_OUT}"

mkdir -p "$(dirname "$OUT")"

if [[ "$INPUT" == "-" ]]; then
  INPUT_ARGS=(--data-binary @-)
else
  INPUT_ARGS=(--data-binary "@${INPUT}")
fi

HTTP_CODE=$(curl -s -o "$OUT" -w "%{http_code}" -X POST \
  "https://api.apify.com/v2/acts/${ACTOR_ID}/run-sync-get-dataset-items" \
  -H "Authorization: Bearer ${APIFY_TOKEN}" \
  -H "Content-Type: application/json" \
  "${INPUT_ARGS[@]}")

if [[ "$HTTP_CODE" != 2* ]]; then
  echo "ERROR: Apify returned HTTP ${HTTP_CODE}. Response saved to ${OUT}" >&2
  exit 1
fi

PY="$REPO_ROOT/.venv/bin/python"; [[ -x "$PY" ]] || PY="$REPO_ROOT/.venv/Scripts/python.exe"; [[ -x "$PY" ]] || PY=python3
COUNT=$("$PY" -c "import json,sys; print(len(json.load(open('$OUT'))))" 2>/dev/null || echo "?")
echo "OK: ${COUNT} items -> ${OUT}"
