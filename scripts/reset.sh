#!/usr/bin/env bash
# Wipe all app data for a clean demo take (refused when ENV=production).
# Run: docker compose exec -T api bash scripts/reset.sh
# Nuclear option (also drops the schema): docker compose down -v
set -euo pipefail
curl -sS -X POST "${BASE_URL:-http://localhost:8000}/admin/reset" \
    -H "X-Admin-Token: ${ADMIN_TOKEN:-dev-admin-token}" | jq .
