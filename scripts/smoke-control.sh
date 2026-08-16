#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
current="$(curl -fsS "$BASE_URL/api/status")"
version="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["control"]["version"])' <<<"$current")"

curl -fsS -X PUT "$BASE_URL/api/audio" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: smoke-muted-$version" \
  --data "{\"mode\":\"MUTED\",\"expected_version\":$version}" >/dev/null

sleep 1
curl -fsS "$BASE_URL/api/status"
echo
