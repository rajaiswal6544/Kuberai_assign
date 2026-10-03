#!/usr/bin/env bash
# Admin helper without JSON quoting (PowerShell-friendly). Sends X-Admin-Token for you.
#   docker compose exec -T api bash scripts/admin.sh price down|up
#   docker compose exec -T api bash scripts/admin.sh run-due [YYYY-MM-DD]   (default: today, IST)
#   docker compose exec -T api bash scripts/admin.sh clock SECONDS
#   docker compose exec -T api bash scripts/admin.sh reset
set -euo pipefail
BASE="${BASE_URL:-http://localhost:8000}"
H=(-H "X-Admin-Token: ${ADMIN_TOKEN:-dev-admin-token}" -H "Content-Type: application/json")
case "${1:-}" in
    price) curl -sS -X POST "${H[@]}" "$BASE/admin/price-source" -d "$(jq -nc --arg m "${2:?up or down}" '{mode: $m}')" | jq -c . ;;
    run-due) curl -sS -X POST "${H[@]}" "$BASE/admin/run-due${2:+?as_of=$2}" | jq -c '{as_of, executed, pending_retry, failed, skipped_missed, catch_up}' ;;
    clock) curl -sS -X POST "${H[@]}" "$BASE/admin/clock" -d "$(jq -nc --argjson s "${2:?seconds}" '{advance_seconds: $s}')" | jq -c . ;;
    reset) curl -sS -X POST "${H[@]}" "$BASE/admin/reset" | jq -c . ;;
    *) sed -n '2,6p' "$0"; exit 2 ;;
esac
