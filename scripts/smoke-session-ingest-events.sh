#!/usr/bin/env bash
set -euo pipefail

tmp_dir="$(mktemp -d)"
override="$tmp_dir/session-events.override.yml"
cookie_jar="$tmp_dir/cookies.txt"
base_url="${BASE_URL:-http://127.0.0.1:8080}"
publisher_pid=""
email="session-events-$(date +%s)-$RANDOM@example.invalid"
password='SmokePassword123!'
bootstrap_token="session-events-node-token"

cat >"$override" <<'YAML'
services:
  control-ui:
    environment:
      NODE_BOOTSTRAP_TOKENS: session-events-node-token
      NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT: "1"
  node-agent:
    environment:
      NODE_BOOTSTRAP_TOKEN: session-events-node-token
      NODE_PROVIDER_SERVER_ID: ${ASSIGNED_PROVIDER_SERVER_ID:-unassigned-provider}
      NODE_BOOT_ID: session-events-boot
      NODE_HEARTBEAT_INTERVAL: "2"
YAML

compose=(docker compose -f docker-compose.poc.yml -f "$override")

cleanup() {
  status=$?
  if [[ -n "$publisher_pid" ]]; then
    kill "$publisher_pid" 2>/dev/null || true
    wait "$publisher_pid" 2>/dev/null || true
  fi
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps >&2 || true
    echo "--- node-agent logs ---" >&2
    "${compose[@]}" logs --no-color --tail=160 node-agent >&2 || true
    echo "--- control logs ---" >&2
    "${compose[@]}" logs --no-color --tail=120 control-ui >&2 || true
    echo "--- mediamtx logs ---" >&2
    "${compose[@]}" logs --no-color --tail=120 mediamtx >&2 || true
    echo "--- publisher log ---" >&2
    tail -100 /tmp/irlight-session-event-publisher.log >&2 2>/dev/null || true
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

login() {
  local response
  rm -f "$cookie_jar"
  response="$(curl -fsS --max-time 10 -c "$cookie_jar" -X POST "$base_url/v1/auth/login" \
    -H 'Content-Type: application/json' \
    --data "{\"email\":\"$email\",\"password\":\"$password\"}")"
  csrf="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])' <<<"$response")"
}

session_json() {
  curl -fsS --max-time 5 -b "$cookie_jar" "$base_url/v1/sessions/$session_id"
}

wait_session_status() {
  local expected="$1"
  local timeout="${2:-40}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    payload="$(session_json 2>/dev/null || true)"
    if python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("status") == sys.argv[1] else 1)' "$expected" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Session did not become $expected" >&2
  session_json >&2 || true
  return 1
}

wait_session_event() {
  local expected="$1"
  local timeout="${2:-40}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    payload="$(curl -fsS --max-time 5 -b "$cookie_jar" "$base_url/v1/sessions/$session_id/events" 2>/dev/null || true)"
    if python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if any(e.get("type") == sys.argv[1] for e in d.get("events", [])) else 1)' "$expected" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Session event not observed: $expected" >&2
  return 1
}

wait_assigned_node() {
  local timeout="${1:-45}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    payload="$(curl -fsS --max-time 5 "$base_url/internal/nodes" 2>/dev/null || true)"
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
  echo "Node did not bind to user Session" >&2
  curl -fsS "$base_url/internal/nodes" >&2 || true
  return 1
}

start_publisher() {
  local duration="${1:-14}"
  "${compose[@]}" exec -T continuity sh -c "
    timeout --signal=INT ${duration}s gst-launch-1.0 -q -e \
      flvmux name=mux streamable=true ! rtmp2sink location='rtmp://mediamtx:1935/live/input?user=${ingest_username}&pass=${ingest_secret}' \
      videotestsrc is-live=true pattern=smpte ! \
        video/x-raw,width=1280,height=720,framerate=30/1,format=I420 ! \
        x264enc tune=zerolatency speed-preset=veryfast bitrate=1200 key-int-max=60 bframes=0 ! \
        video/x-h264,profile=main ! h264parse config-interval=-1 ! queue ! mux. \
      audiotestsrc is-live=true wave=sine freq=440 ! audioconvert ! audioresample ! \
        audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=128000 ! aacparse ! queue ! mux.
  " >/tmp/irlight-session-event-publisher.log 2>&1 &
  publisher_pid=$!
}

"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
# Start the Control Plane/media dependencies without Node Agent. This lets the
# user Session allocate its provider_server_id before the Node bootstraps.
"${compose[@]}" up -d --build control-ui
wait_http "$base_url/healthz" 60

curl -fsS --max-time 10 -X POST "$base_url/v1/auth/register" \
  -H 'Content-Type: application/json' \
  --data "{\"email\":\"$email\",\"password\":\"$password\",\"display_name\":\"Session Events Smoke\"}" >/dev/null
login

session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
prepared="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/prepare" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  -H "Idempotency-Key: session-events-$session_id" \
  --data '{"environment":"dev"}')"
provider_server_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["provider_server_id"])' <<<"$prepared")"
if [[ -z "$provider_server_id" || "$provider_server_id" == "None" ]]; then
  echo "prepare did not allocate provider_server_id" >&2
  exit 1
fi

export ASSIGNED_PROVIDER_SERVER_ID="$provider_server_id"
"${compose[@]}" up -d --build node-agent
wait_assigned_node 45

assigned="$(session_json)"
python3 -c '
import json,sys
item=json.load(sys.stdin)
assert item.get("node_id"), item
assert item.get("node_boot_id") == "session-events-boot", item
assert item.get("node_registered_at") is not None, item
' <<<"$assigned"

credential="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/ingest-credentials" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  --data '{"protocols":["rtmp"],"ttl_seconds":3600}')"
ingest_username="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])' <<<"$credential")"
ingest_secret="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["credential_secret"])' <<<"$credential")"

start_publisher 14
wait_session_status LIVE 45
wait_session_event ingest.connected 30
wait_session_event ingest.format_detected 30

live="$(session_json)"
python3 -c '
import json,sys
item=json.load(sys.stdin)
assert item.get("first_ingest_at") is not None, item
assert item.get("last_ingest_at") is not None, item
' <<<"$live"

wait "$publisher_pid" || true
publisher_pid=""
wait_session_status HOLDING 30
wait_session_event ingest.disconnected 30

events="$(curl -fsS --max-time 5 -b "$cookie_jar" "$base_url/v1/sessions/$session_id/events")"
python3 -c '
import json,sys
d=json.load(sys.stdin)
events=d.get("events", [])
required={"ingest.connected", "ingest.format_detected", "ingest.disconnected"}
assert required.issubset({e.get("type") for e in events}), events
for event in events:
    if event.get("type", "").startswith("ingest."):
        assert event.get("origin") == "node-agent", event
        payload=event.get("payload", {})
        assert payload.get("node_id"), event
        forbidden={"credential_secret", "password", "token"}
        assert forbidden.isdisjoint(payload), event
' <<<"$events"

echo "IRLight Session ingest event integration smoke passed."
