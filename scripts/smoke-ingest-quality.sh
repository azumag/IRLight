#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/node-admin.sh"

tmp_dir="$(mktemp -d)"
override="$tmp_dir/ingest-quality.override.yml"
cat >"$override" <<'YAML'
services:
  control-ui:
    environment:
      NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT: "1"
  node-agent:
    environment:
      NODE_PROVIDER_SERVER_ID: ${ASSIGNED_PROVIDER_SERVER_ID:-unassigned-provider}
      NODE_BOOT_ID: ingest-quality-smoke-boot
YAML

compose=(docker compose -f docker-compose.poc.yml -f "$override")
base_url="${BASE_URL:-http://127.0.0.1:8080}"
cookie_jar="/tmp/irlight-quality-cookies.txt"
publisher_pid=""
ingest_username=""
ingest_secret=""
provider_server_id=""
session_id=""

cleanup() {
  status=$?
  if [[ -n "$publisher_pid" ]]; then
    kill "$publisher_pid" 2>/dev/null || true
    wait "$publisher_pid" 2>/dev/null || true
  fi
  if [[ $status -ne 0 ]]; then
    echo "--- node-agent logs ---" >&2
    "${compose[@]}" logs --no-color --tail=200 node-agent >&2 || true
    echo "--- mediamtx logs ---" >&2
    "${compose[@]}" logs --no-color --tail=120 mediamtx >&2 || true
    echo "--- control logs ---" >&2
    "${compose[@]}" logs --no-color --tail=120 control-ui >&2 || true
  fi
  rm -f "$cookie_jar"
  "${compose[@]}" down --remove-orphans >/dev/null 2>&1 || true
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
      echo "HTTP timeout: $url" >&2
      return 1
    fi
    sleep 1
  done
}

wait_assigned_node() {
  local timeout="${1:-45}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    payload="$(node_admin_curl -fsS --max-time 3 "$base_url/internal/nodes" 2>/dev/null || true)"
    if python3 -c '
import json,sys
session_id=sys.argv[1]
d=json.load(sys.stdin)
raise SystemExit(0 if any(n.get("session_assigned") is True and n.get("session_id") == session_id for n in d.get("nodes", {}).values()) else 1)
' "$session_id" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Node did not bind to ingest quality Session" >&2
  return 1
}

wait_node_status() {
  local expected="$1"
  local reason="${2:-}"
  local timeout="${3:-40}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    payload="$(node_admin_curl -fsS --max-time 3 "$base_url/internal/nodes" 2>/dev/null || true)"
    if python3 -c '
import json, sys
expected, reason = sys.argv[1], sys.argv[2]
d = json.load(sys.stdin)
for node in d.get("nodes", {}).values():
    ingest = node.get("ingest") or {}
    if ingest.get("status") == expected and (not reason or reason in ingest.get("reasons", [])):
        raise SystemExit(0)
raise SystemExit(1)
' "$expected" "$reason" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Node ingest status did not become $expected reason=$reason" >&2
  node_admin_curl -fsS "$base_url/internal/nodes" >&2 || true
  return 1
}

wait_node_event() {
  local expected="$1"
  local timeout="${2:-30}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    payload="$(node_admin_curl -fsS --max-time 3 "$base_url/internal/nodes" 2>/dev/null || true)"
    if python3 -c '
import json, sys
expected = sys.argv[1]
d = json.load(sys.stdin)
for node in d.get("nodes", {}).values():
    if any(event.get("type") == expected for event in node.get("events", [])):
        raise SystemExit(0)
raise SystemExit(1)
' "$expected" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Node event not found: $expected" >&2
  return 1
}

setup_ingest() {
  local email password login csrf credential prepared
  email="quality-$(date +%s)-$RANDOM@example.invalid"
  password="QualitySmoke123!"
  rm -f "$cookie_jar"

  curl -fsS --max-time 10 -X POST "$base_url/v1/auth/register" \
    -H 'Content-Type: application/json' \
    --data "{\"email\":\"$email\",\"password\":\"$password\",\"display_name\":\"Quality Smoke\"}" >/dev/null
  login="$(curl -fsS --max-time 10 -c "$cookie_jar" -X POST "$base_url/v1/auth/login" \
    -H 'Content-Type: application/json' \
    --data "{\"email\":\"$email\",\"password\":\"$password\"}")"
  csrf="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])' <<<"$login")"
  session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"

  prepared="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
    "$base_url/v1/sessions/$session_id/prepare" \
    -H 'Content-Type: application/json' \
    -H "X-CSRF-Token: $csrf" \
    -H "Idempotency-Key: quality-$session_id" \
    --data '{"environment":"dev"}')"
  provider_server_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["provider_server_id"])' <<<"$prepared")"

  credential="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
    "$base_url/v1/sessions/$session_id/ingest-credentials" \
    -H 'Content-Type: application/json' \
    -H "X-CSRF-Token: $csrf" \
    --data '{"protocols":["rtmp"],"ttl_seconds":3600}')"
  ingest_username="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])' <<<"$credential")"
  ingest_secret="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["credential_secret"])' <<<"$credential")"
}

start_publisher() {
  local fps="$1"
  local duration="${2:-35}"
  local key_int=$((fps * 2))
  "${compose[@]}" exec -T continuity sh -c "
    timeout --signal=INT --kill-after=5s ${duration}s gst-launch-1.0 -q -e \
      flvmux name=mux streamable=true ! \
        rtmp2sink location='rtmp://mediamtx:1935/live/input?user=${ingest_username}&pass=${ingest_secret}' \
      videotestsrc is-live=true pattern=smpte ! \
        video/x-raw,width=1280,height=720,framerate=${fps}/1,format=I420 ! \
        x264enc tune=zerolatency speed-preset=veryfast bitrate=1200 key-int-max=${key_int} bframes=0 ! \
        video/x-h264,profile=main ! h264parse config-interval=-1 ! queue ! mux. \
      audiotestsrc is-live=true wave=sine freq=440 ! audioconvert ! audioresample ! \
        audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=128000 ! aacparse ! queue ! mux.
  " >/tmp/irlight-quality-publisher.log 2>&1 &
  publisher_pid=$!
}

"${compose[@]}" down --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" config >/dev/null
# User ingest authorization is node-bound after hardening. Prepare the fake
# provider Session first, then bootstrap a Node with its assigned server id.
"${compose[@]}" up -d --build control-ui
wait_http "$base_url/healthz" 90
setup_ingest
export ASSIGNED_PROVIDER_SERVER_ID="$provider_server_id"
"${compose[@]}" up -d --build node-agent
wait_assigned_node 45

# 10fps is valid H.264/AAC/720p, so the hard policy must not kick it. The
# quality sampler should instead surface DEGRADED/FPS_OUT_OF_RANGE.
# Let this publisher stop through its in-container timeout. Killing the local
# `docker compose exec` client can leave gst-launch alive inside the container
# and would make an OFFLINE assertion test the client process instead of the
# actual ingest lifecycle.
start_publisher 10 18
wait_node_status DEGRADED FPS_OUT_OF_RANGE 45
wait_node_event ingest.degraded 20
if ! kill -0 "$publisher_pid" 2>/dev/null; then
  echo "DEGRADED publisher was unexpectedly kicked" >&2
  exit 1
fi
wait "$publisher_pid" 2>/dev/null || true
publisher_pid=""
wait_node_status OFFLINE "" 25

# A fresh standards-compliant publisher must be accepted after the degraded
# stream disconnects.
start_publisher 30 30
wait_node_status ACCEPTED "" 45
if ! kill -0 "$publisher_pid" 2>/dev/null; then
  echo "healthy publisher exited before ACCEPTED" >&2
  exit 1
fi

echo "IRLight ingest quality smoke test passed."
