#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/node-admin.sh"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
umask 077
tmp_dir="$(mktemp -d)"
override="$tmp_dir/disconnect-recovery.override.yml"
cookie_jar="$tmp_dir/cookies.txt"
publisher_log="$tmp_dir/publisher.log"
base_url="${BASE_URL:-http://127.0.0.1:8080}"
disconnect_seconds="${DISCONNECT_SECONDS:-10}"
recovery_stable_seconds="${RECOVERY_STABLE_SECONDS:-3}"
publisher_pid=""
email="disconnect-recovery-$(date +%s)-$RANDOM@example.invalid"
password='SmokePassword123!'

case "$disconnect_seconds" in
  10|30|120|600) ;;
  *)
    echo "DISCONNECT_SECONDS must be one of: 10, 30, 120, 600" >&2
    exit 2
    ;;
esac

cat >"$override" <<YAML
services:
  control-ui:
    environment:
      NODE_BOOTSTRAP_TOKENS: disconnect-recovery-node-token
      NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT: "1"
      RECOVERY_STABLE_SECONDS: "$recovery_stable_seconds"
  node-agent:
    environment:
      NODE_BOOTSTRAP_TOKEN: disconnect-recovery-node-token
      NODE_PROVIDER_SERVER_ID: \${ASSIGNED_PROVIDER_SERVER_ID:-unassigned-provider}
      NODE_BOOT_ID: disconnect-recovery-boot
      NODE_HEARTBEAT_INTERVAL: "2"
      NODE_INGEST_SAMPLE_SECONDS: "2"
      NODE_INGEST_SAMPLE_TIMEOUT_MARGIN_SECONDS: "2"
YAML

smoke_project="irlight-ingest-disconnect-recovery-smoke-$$-$RANDOM"
compose=(docker compose -p "$smoke_project" -f "$repo_root/docker-compose.poc.yml" -f "$override")

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps >&2 || true
    echo "--- node-agent logs ---" >&2
    "${compose[@]}" logs --no-color --tail=180 node-agent >&2 || true
    echo "--- control logs ---" >&2
    "${compose[@]}" logs --no-color --tail=160 control-ui >&2 || true
    echo "--- mediamtx logs ---" >&2
    "${compose[@]}" logs --no-color --tail=120 mediamtx >&2 || true
    echo "--- publisher log ---" >&2
    cat "$publisher_log" >&2 2>/dev/null || true
  fi
  if [[ -n "$publisher_pid" ]]; then
    kill "$publisher_pid" 2>/dev/null || true
    wait "$publisher_pid" 2>/dev/null || true
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
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

session_events() {
  curl -fsS --max-time 5 -b "$cookie_jar" "$base_url/v1/sessions/$session_id/events"
}

wait_session_status() {
  local expected="$1"
  local timeout="${2:-60}"
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
  session_events >&2 || true
  return 1
}

wait_holding_reason() {
  local expected="$1"
  local timeout="${2:-60}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    status_payload="$(session_json 2>/dev/null || true)"
    events_payload="$(session_events 2>/dev/null || true)"
    if python3 -c '
import json,sys
expected=sys.argv[1]
session=json.loads(sys.argv[2])
events=json.load(sys.stdin).get("events", [])
ok=session.get("status") == "HOLDING" and any(
    e.get("type") == "session.holding" and e.get("reason_code") == expected
    for e in events
)
raise SystemExit(0 if ok else 1)
' "$expected" "$status_payload" <<<"$events_payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Session did not enter HOLDING with reason $expected" >&2
  session_json >&2 || true
  session_events >&2 || true
  return 1
}

wait_assigned_node() {
  local timeout="${1:-45}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    payload="$(node_admin_curl -fsS --max-time 5 "$base_url/internal/nodes" 2>/dev/null || true)"
    if python3 -c '
import json,sys
session_id=sys.argv[1]
d=json.load(sys.stdin)
raise SystemExit(0 if any(
    n.get("session_assigned") is True and n.get("session_id") == session_id
    for n in d.get("nodes", {}).values()
) else 1)
' "$session_id" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Node did not bind to user Session" >&2
  node_admin_curl -fsS "$base_url/internal/nodes" >&2 || true
  return 1
}

wait_recovery_candidate() {
  local timeout="${1:-45}"
  local deadline=$((SECONDS + timeout))
  local payload candidate
  while (( SECONDS < deadline )); do
    payload="$(session_json 2>/dev/null || true)"
    candidate="$(python3 -c '
import json,sys
d=json.load(sys.stdin)
since=d.get("recovery_candidate_since")
source=d.get("recovery_candidate_source_id")
if d.get("status") == "HOLDING" and since is not None and source:
    print(f"{since}\t{source}")
else:
    raise SystemExit(1)
' <<<"$payload" 2>/dev/null || true)"
    if [[ -n "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
    sleep 0.25
  done
  echo "Recovery candidate did not become observable while HOLDING" >&2
  session_json >&2 || true
  session_events >&2 || true
  return 1
}

stop_publisher() {
  "${compose[@]}" exec -T continuity sh -c 'touch /tmp/irlight-stop-disconnect-publisher'
  if [[ -n "$publisher_pid" ]]; then
    wait "$publisher_pid" 2>/dev/null || true
    publisher_pid=""
  fi
}

start_publisher() {
  "${compose[@]}" exec -T continuity sh -c 'rm -f /tmp/irlight-stop-disconnect-publisher'
  "${compose[@]}" exec -T \
    -e IRLIGHT_PUBLISH_USER="$ingest_username" \
    -e IRLIGHT_PUBLISH_PASS="$ingest_secret" \
    continuity python3 - <<'PY' >"$publisher_log" 2>&1 &
from __future__ import annotations

import os
import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)
user = os.environ["IRLIGHT_PUBLISH_USER"]
password = os.environ["IRLIGHT_PUBLISH_PASS"]
url = f"rtmp://mediamtx:1935/live/input?user={user}&pass={password}"
pipeline = Gst.parse_launch(
    f'''flvmux name=mux streamable=true ! rtmp2sink location="{url}"
    videotestsrc is-live=true pattern=smpte !
      video/x-raw,width=1280,height=720,framerate=30/1,format=I420 !
      x264enc tune=zerolatency speed-preset=veryfast bitrate=1200 key-int-max=60 bframes=0 !
      video/x-h264,profile=main ! h264parse config-interval=-1 ! queue ! mux.
    audiotestsrc is-live=true wave=sine freq=440 ! audioconvert ! audioresample !
      audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=128000 ! aacparse ! queue ! mux.'''
)
bus = pipeline.get_bus()
if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
    raise SystemExit("publisher failed to start")
try:
    while not Path("/tmp/irlight-stop-disconnect-publisher").exists():
        message = bus.timed_pop_filtered(
            100 * Gst.MSECOND,
            Gst.MessageType.ERROR | Gst.MessageType.EOS,
        )
        if message is not None:
            raise SystemExit("publisher terminated unexpectedly")
        time.sleep(0.05)
finally:
    pipeline.set_state(Gst.State.NULL)
PY
  publisher_pid=$!
}

assert_recovery_sequence() {
  local candidate_since="$1"
  local candidate_source_id="$2"
  local events_payload
  events_payload="$(session_events)"
  python3 -c '
import json
import sys

stable = float(sys.argv[1])
candidate_since = float(sys.argv[2])
candidate_source_id = sys.argv[3]
events = json.load(sys.stdin).get("events", [])
holding_indexes = [
    i for i, e in enumerate(events)
    if e.get("type") == "session.holding" and e.get("reason_code") == "INGEST_DISCONNECTED"
]
if not holding_indexes:
    raise SystemExit("missing INGEST_DISCONNECTED session.holding event")
start = holding_indexes[-1]
window = events[start + 1:]
reconnected = next((e for e in window if e.get("type") == "ingest.reconnected"), None)
recovered = next((e for e in window if e.get("type") == "session.recovered"), None)
if reconnected is None:
    raise SystemExit("missing ingest.reconnected after disconnect")
if recovered is None:
    raise SystemExit("missing session.recovered after disconnect")
reconnected_source = (reconnected.get("payload") or {}).get("source_id")
recovered_source = (recovered.get("payload") or {}).get("source_id")
if reconnected_source != candidate_source_id:
    raise SystemExit(f"candidate source differs from reconnect source: {candidate_source_id} != {reconnected_source}")
if recovered_source != candidate_source_id:
    raise SystemExit(f"recovered source differs from candidate source: {candidate_source_id} != {recovered_source}")
delta = float(recovered["occurred_at"]) - candidate_since
minimum = max(0.0, stable - 0.5)
if delta < minimum:
    raise SystemExit(f"recovery stability window too short: {delta:.3f}s < {minimum:.3f}s")
print(f"recovery stability candidate gap: {delta:.3f}s")
' "$recovery_stable_seconds" "$candidate_since" "$candidate_source_id" <<<"$events_payload"
}

"${compose[@]}" config >/dev/null
"${compose[@]}" up -d --build control-ui
wait_http "$base_url/healthz" 60

curl -fsS --max-time 10 -X POST "$base_url/v1/auth/register" \
  -H 'Content-Type: application/json' \
  --data "{\"email\":\"$email\",\"password\":\"$password\",\"display_name\":\"Disconnect Recovery Smoke\"}" >/dev/null
login

session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
prepared="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/prepare" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  -H "Idempotency-Key: disconnect-recovery-$session_id" \
  --data '{"environment":"dev"}')"
provider_server_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["provider_server_id"])' <<<"$prepared")"
if [[ -z "$provider_server_id" || "$provider_server_id" == "None" ]]; then
  echo "prepare did not allocate provider_server_id" >&2
  exit 1
fi

export ASSIGNED_PROVIDER_SERVER_ID="$provider_server_id"
"${compose[@]}" up -d --build node-agent
wait_assigned_node 45

credential="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/ingest-credentials" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  --data '{"protocols":["rtmp"],"ttl_seconds":1800}')"
ingest_username="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])' <<<"$credential")"
ingest_secret="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["credential_secret"])' <<<"$credential")"

start_publisher
wait_session_status LIVE 60
if ! kill -0 "$publisher_pid" 2>/dev/null; then
  echo "publisher exited before initial LIVE" >&2
  exit 1
fi

stop_publisher
wait_holding_reason INGEST_DISCONNECTED 60
holding_payload="$(session_json)"
python3 -c '
import json,sys
s=json.load(sys.stdin)
assert s.get("status") == "HOLDING"
# hold_deadline_at is initialized by the reaper, not by the ingest transition itself.
assert s.get("last_ingest_at") is not None
assert not any(e.get("type") in {"session.failed", "session.finished"} for e in s.get("events", []))
' <<<"$holding_payload"

echo "holding for ${disconnect_seconds}s before publisher reconnect"
sleep "$disconnect_seconds"

pre_reconnect="$(session_json)"
python3 -c '
import json,sys
s=json.load(sys.stdin)
assert s.get("status") == "HOLDING", s.get("status")
assert not any(e.get("type") in {"session.failed", "session.finished"} for e in s.get("events", []))
' <<<"$pre_reconnect"

start_publisher
candidate_info="$(wait_recovery_candidate 45)"
candidate_since="${candidate_info%%$'\t'*}"
candidate_source_id="${candidate_info#*$'\t'}"
wait_session_status LIVE 75
if ! kill -0 "$publisher_pid" 2>/dev/null; then
  echo "publisher exited before recovered LIVE" >&2
  exit 1
fi
assert_recovery_sequence "$candidate_since" "$candidate_source_id"

final_payload="$(session_json)"
python3 -c '
import json,sys
s=json.load(sys.stdin)
assert s.get("status") == "LIVE"
assert s.get("failure_reason_code") is None
' <<<"$final_payload"

echo "ingest disconnect recovery smoke passed (${disconnect_seconds}s disconnect)"
