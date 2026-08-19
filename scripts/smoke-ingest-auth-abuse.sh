#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -f docker-compose.poc.yml)
base_url="${BASE_URL:-http://127.0.0.1:8080}"
username="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
source_ip="203.0.113.50"
wrong_secret="abuse-secret-must-never-persist"
headers_file="/tmp/irlight-auth-abuse-headers.txt"
body_file="/tmp/irlight-auth-abuse-body.json"

export IRLIGHT_INGEST_AUTH_FAILURE_WINDOW_SECONDS=60
export IRLIGHT_INGEST_AUTH_MAX_FAILURES_PER_CREDENTIAL=3
export IRLIGHT_INGEST_AUTH_MAX_FAILURES_PER_IP=20
export IRLIGHT_INGEST_AUTH_LOCKOUT_SECONDS=30

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- control-ui logs ---" >&2
    "${compose[@]}" logs --no-color --tail=100 control-ui >&2 || true
    echo "--- response headers ---" >&2
    cat "$headers_file" >&2 2>/dev/null || true
    echo "--- response body ---" >&2
    cat "$body_file" >&2 2>/dev/null || true
  fi
  rm -f "$headers_file" "$body_file"
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

wait_http() {
  local deadline=$((SECONDS + 45))
  until curl -fsS --max-time 3 "$base_url/healthz" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "control API did not become healthy" >&2
      return 1
    fi
    sleep 1
  done
}

auth_status() {
  curl -sS \
    -D "$headers_file" \
    -o "$body_file" \
    -w '%{http_code}' \
    --max-time 5 \
    -X POST "$base_url/internal/ingest/auth" \
    -H 'Content-Type: application/json' \
    --data "{\"user\":\"$username\",\"password\":\"$wrong_secret\",\"ip\":\"$source_ip\",\"action\":\"publish\",\"path\":\"live/input\",\"protocol\":\"rtmp\",\"id\":\"abuse-smoke\"}"
}

"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" up -d --build control-ui
wait_http

for attempt in 1 2; do
  status="$(auth_status)"
  if [[ "$status" != "401" ]]; then
    echo "attempt $attempt expected HTTP 401, got $status" >&2
    exit 1
  fi
done

status="$(auth_status)"
if [[ "$status" != "429" ]]; then
  echo "threshold attempt expected HTTP 429, got $status" >&2
  exit 1
fi
if ! grep -qi '^retry-after:' "$headers_file"; then
  echo "HTTP 429 did not include Retry-After" >&2
  exit 1
fi

status="$(auth_status)"
if [[ "$status" != "429" ]]; then
  echo "locked attempt expected HTTP 429, got $status" >&2
  exit 1
fi

state="$("${compose[@]}" exec -T control-ui cat /state/ingest_auth_guard.json)"
if grep -Fq "$wrong_secret" <<<"$state"; then
  echo "raw ingest secret leaked into auth guard state" >&2
  exit 1
fi
python3 -c '
import json, sys
state = json.load(sys.stdin)
types = [event.get("type") for event in state.get("events", [])]
required = {"ingest.auth_failed", "ingest.auth_locked", "ingest.auth_blocked"}
missing = sorted(required.difference(types))
if missing:
    raise SystemExit(f"missing auth guard events: {missing}")
if len(state.get("buckets", {})) > 4096:
    raise SystemExit("auth guard bucket bound exceeded")
' <<<"$state"

echo "IRLight ingest auth abuse smoke passed."
