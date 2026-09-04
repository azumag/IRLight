#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/node-admin.sh"

soak_seconds="${SOAK_SECONDS:-600}"
interval_seconds="${SOAK_INTERVAL_SECONDS:-30}"
if [[ ! "$soak_seconds" =~ ^[1-9][0-9]*$ ]]; then
  echo "SOAK_SECONDS must be a positive integer" >&2
  exit 2
fi
if [[ ! "$interval_seconds" =~ ^[1-9][0-9]*$ ]]; then
  echo "SOAK_INTERVAL_SECONDS must be a positive integer" >&2
  exit 2
fi

soak_project="${IRLIGHT_SOAK_PROJECT:-irlight-poc-soak-$$-$RANDOM}"
compose=(docker compose -p "$soak_project" -f docker-compose.poc.yml)
base_url="${BASE_URL:-http://127.0.0.1:8080}"
hls_url="${HLS_URL:-http://127.0.0.1:8888/output/relay/index.m3u8}"

cleanup() {
  local status=$?
  "${compose[@]}" down --rmi local --volumes --remove-orphans >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

wait_http() {
  local url="$1"
  local deadline=$((SECONDS + 90))
  until curl -fsS --max-time 5 "$url" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "HTTP endpoint did not become ready: $url" >&2
      return 1
    fi
    sleep 1
  done
}

wait_node() {
  local deadline=$((SECONDS + 90))
  local payload
  while (( SECONDS < deadline )); do
    payload="$(node_admin_curl -fsS --max-time 5 "$base_url/internal/nodes" 2>/dev/null || true)"
    if python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("nodes") else 1)' \
      <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Node Agent did not register before the soak" >&2
  return 1
}

"${compose[@]}" up -d --build
wait_http "$base_url/api/status"
wait_http "$hls_url"
wait_node

deadline=$((SECONDS + soak_seconds))
checks=0
while (( SECONDS < deadline )); do
  curl -fsS --max-time 5 "$base_url/api/status" >/dev/null
  curl -fsS --max-time 5 "$hls_url" >/dev/null
  node_admin_curl -fsS --max-time 5 "$base_url/internal/nodes" |
    python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("nodes") else 1)'

  running_services="$("${compose[@]}" ps --status running --services | wc -l | tr -d ' ')"
  if [[ "$running_services" != "4" ]]; then
    echo "expected 4 running services, got $running_services" >&2
    "${compose[@]}" ps >&2
    exit 1
  fi
  checks=$((checks + 1))

  remaining=$((deadline - SECONDS))
  if (( remaining <= 0 )); then
    break
  fi
  if (( remaining < interval_seconds )); then
    sleep "$remaining"
  else
    sleep "$interval_seconds"
  fi
done

echo "IRLight compose soak passed: ${soak_seconds}s, ${checks} checks."
