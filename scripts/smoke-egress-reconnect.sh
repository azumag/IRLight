#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp_dir="$(mktemp -d)"
override="$tmp_dir/egress-reconnect.override.yml"
secret_file="$tmp_dir/egress_url"
stream_key="ci-egress-secret-$RANDOM"
export EGRESS_SECRET_FILE="$secret_file"

cat >"$secret_file" <<EOF
rtmp://egress-target:1935/live/$stream_key
EOF
chmod 600 "$secret_file"

cat >"$override" <<'YAML'
services:
  egress-target:
    image: bluenviron/mediamtx:1.20.0
    restart: "no"
    environment:
      MTX_API: "yes"
      MTX_APIADDRESS: ":9997"
    ports:
      - "127.0.0.1:19997:9997"

  egress-gateway:
    build:
      context: ./apps/egress-gateway
      dockerfile: Dockerfile
    restart: "no"
    depends_on:
      - mediamtx
      - continuity
      - egress-target
    environment:
      EGRESS_INPUT_URI: rtsp://mediamtx:8554/output/relay
      EGRESS_URL_FILE: /run/irlight/secrets/egress_url
      EGRESS_STATUS_FILE: /state/egress.json
      EGRESS_CONNECT_TIMEOUT_SECONDS: "10"
      EGRESS_RETRY_INITIAL_SECONDS: "1"
      EGRESS_RETRY_MAX_SECONDS: "2"
      EGRESS_RETRY_MULTIPLIER: "2"
      EGRESS_RETRY_JITTER_RATIO: "0"
      EGRESS_MAX_ATTEMPTS: "0"
      EGRESS_MAX_RETRY_SECONDS: "0"
    volumes:
      - irlight-state:/state
      - ${EGRESS_SECRET_FILE}:/run/irlight/secrets/egress_url:ro
YAML

compose=(docker compose -f "$repo_root/docker-compose.poc.yml" -f "$override")

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps >&2 || true
    echo "--- continuity logs ---" >&2
    "${compose[@]}" logs --no-color --tail=120 continuity >&2 || true
    echo "--- egress gateway logs ---" >&2
    "${compose[@]}" logs --no-color --tail=160 egress-gateway >&2 || true
    echo "--- target logs ---" >&2
    "${compose[@]}" logs --no-color --tail=120 egress-target >&2 || true
  fi
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT

wait_http() {
  local url="$1"
  local timeout="${2:-60}"
  local deadline=$((SECONDS + timeout))
  until curl -fsS --max-time 3 "$url" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "HTTP endpoint did not become ready: $url" >&2
      return 1
    fi
    sleep 1
  done
}

read_egress_status() {
  "${compose[@]}" exec -T egress-gateway cat /state/egress.json 2>/dev/null || true
}

wait_egress_status() {
  local expected="$1"
  local timeout="${2:-45}"
  local deadline=$((SECONDS + timeout))
  local payload=""
  while (( SECONDS < deadline )); do
    payload="$(read_egress_status)"
    if python3 -c '
import json,sys
expected=sys.argv[1]
try:
    value=json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if value.get("status") == expected else 1)
' "$expected" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "egress status did not become $expected; last=$payload" >&2
  return 1
}

wait_target_path() {
  local timeout="${1:-45}"
  local deadline=$((SECONDS + timeout))
  local payload=""
  while (( SECONDS < deadline )); do
    payload="$(curl -fsS --max-time 3 http://127.0.0.1:19997/v3/paths/list 2>/dev/null || true)"
    if python3 -c '
import json,sys
name=sys.argv[1]
try:
    value=json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
items=value.get("items", [])
raise SystemExit(0 if any(item.get("name") == name and item.get("ready") is True for item in items) else 1)
' "live/$stream_key" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "target did not receive live/$stream_key" >&2
  return 1
}

"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" up -d --build mediamtx continuity egress-target egress-gateway
wait_http http://127.0.0.1:19997/v3/paths/list 60
wait_egress_status CONNECTED 60
wait_target_path 60

status_payload="$(read_egress_status)"
if grep -Fq "$stream_key" <<<"$status_payload"; then
  echo "egress status leaked stream key" >&2
  exit 1
fi
if "${compose[@]}" logs --no-color egress-gateway | grep -Fq "$stream_key"; then
  echo "egress logs leaked stream key" >&2
  exit 1
fi

# Simulate a remote RTMP outage. The Egress Gateway must reconnect on its own;
# Continuity must keep publishing the local output/relay stream throughout.
"${compose[@]}" stop egress-target >/dev/null
wait_egress_status RECONNECTING 45
if ! "${compose[@]}" ps --status running --services | grep -qx continuity; then
  echo "continuity stopped when the external destination went down" >&2
  exit 1
fi

"${compose[@]}" start egress-target >/dev/null
wait_http http://127.0.0.1:19997/v3/paths/list 45
wait_egress_status CONNECTED 60
wait_target_path 60

if ! "${compose[@]}" ps --status running --services | grep -qx continuity; then
  echo "continuity is not running after egress recovery" >&2
  exit 1
fi

echo "IRLight egress reconnect smoke passed."
