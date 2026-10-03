#!/usr/bin/env bash
# Send one chat message and print the reply (PowerShell-friendly: no JSON quoting needed).
# Run: docker compose exec -T api bash scripts/chat.sh demo-user "Buy gold worth 500"
set -euo pipefail
USER_ID="$1"
shift
curl -sS -X POST "${BASE_URL:-http://localhost:8000}/chat" \
    -H "X-User-Id: $USER_ID" -H "Content-Type: application/json" \
    -d "$(jq -nc --arg m "$*" '{message: $m}')" |
    jq -r '"[\(.intent) | \(.language)\(if .error then " | error=" + .error else "" end)]\n\(.reply)" +
           (if (.suggested_actions | length) > 0 then "\n  chips: " + (.suggested_actions | join(" / ")) else "" end)'
