#!/usr/bin/env bash
set -euo pipefail

tmp_dir="$(mktemp -d)"
override="$tmp_dir/rtmp-netem.override.yml"
cookie_jar="$tmp_dir/cookies.txt"
base_url="${BASE_URL:-http://127.0.0.1:8080}"
fault_seconds="${NETEM_FAULT_SECONDS:-10}"
publisher_name="irlight-netem-publisher-$RANDOM-$RANDOM"
publisher_pid=""
netem_applied=0
email="rtmp-netem-$(date +%s)-$RANDOM@example.invalid"
password='SmokePassword123!'

case "$fault_seconds" in
  ''|*[!0-9]*)
    echo "NETEM_FAULT_SECONDS must be a positive integer" >&2
    exit 2
    ;;
esac
if (( fault_seconds < 5 || fault_seconds > 60 )); then
  echo "NETEM_FAULT_SECONDS must be between 5 and 60" >&2
  exit 2
fi

stage() {
  printf '\n=== rtmp-netem stage: %s ===\n' "$1"
}

cat >"$override" <<'YAML'
services:
  control-ui:
    environment:
      NODE_BOOTSTRAP_TOKENS: rtmp-netem-node-token
      NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT: "1"
      RECOVERY_STABLE_SECONDS: "3"
  node-agent:
    environment:
      NODE_BOOTSTRAP_TOKEN: rtmp-netem-node-token
      NODE_PROVIDER_SERVER_ID: ${ASSIGNED_PROVIDER_SERVER_ID:-unassigned-provider}
      NODE_BOOT_ID: rtmp-netem-boot
      NODE_HEARTBEAT_INTERVAL: "2"
      NODE_INGEST_SAMPLE_SECONDS: "2"
      NODE_INGEST_SAMPLE_TIMEOUT_MARGIN_SECONDS: "2"
YAML

compose=(docker compose -f docker-compose.poc.yml -f "$override")

cleanup() {
  status=$?
  if (( netem_applied == 1 )); then
    bash ./scripts/netem-container.sh clear "$publisher_name" >/dev/null 2>&1 || true
  fi
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps >&2 || true
    echo "--- publisher log ---" >&2
    cat /tmp/irlight-rtmp-netem-publisher.log >&2 2>/dev/null || true
    echo "--- continuity status ---" >&2
    "${compose[@]}" exec -T continuity sh -c 'cat /state/status.json 2>/dev/null || true' >&2 || true
    echo "--- node-agent logs ---" >&2
    "${compose[@]}" logs --no-color --tail=180 node-agent >&2 || true
    echo "--- continuity logs ---" >&2
    "${compose[@]}" logs --no-color --tail=160 continuity >&2 || true
    echo "--- control logs ---" >&2
    "${compose[@]}" logs --no-color --tail=160 control-ui >&2 || true
    echo "--- mediamtx logs ---" >&2
    "${compose[@]}" logs --no-color --tail=160 mediamtx >&2 || true
  fi
  if [[ -n "$publisher_pid" ]]; then
    kill "$publisher_pid" 2>/dev/null || true
    wait "$publisher_pid" 2>/dev/null || true
  fi
  docker rm -f "$publisher_name" >/dev/null 2>&1 || true
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$cookie_jar" /tmp/irlight-rtmp-netem-publisher.log
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

latest_event_sequence() {
  session_events | python3 -c '
import json,sys
payload=json.load(sys.stdin)
events=payload if isinstance(payload, list) else payload.get("events", [])
print(max((int(e.get("sequence", 0)) for e in events), default=0))
'
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

wait_fault_holding_after() {
  local after_sequence="$1"
  local timeout="${2:-60}"
  local deadline=$((SECONDS + timeout))
  local result
  while (( SECONDS < deadline )); do
    status_payload="$(session_json 2>/dev/null || true)"
    events_payload="$(session_events 2>/dev/null || true)"
    result="$(python3 -c '
import json,sys
session=json.loads(sys.argv[1])
after=int(sys.argv[2])
payload=json.load(sys.stdin)
events=payload if isinstance(payload, list) else payload.get("events", [])
allowed={"VIDEO_TIMEOUT", "AUDIO_TIMEOUT", "INGEST_DISCONNECTED"}
match=next((
    e for e in events
    if int(e.get("sequence", 0)) > after
    and e.get("type") == "session.holding"
    and e.get("reason_code") in allowed
), None)
if session.get("status") == "HOLDING" and match is not None:
    print(match.get("reason_code"))
else:
    raise SystemExit(1)
' "$status_payload" "$after_sequence" <<<"$events_payload" 2>/dev/null || true)"
    if [[ -n "$result" ]]; then
      printf '%s\n' "$result"
      return 0
    fi
    sleep 1
  done
  echo "Session did not enter HOLDING after netem blackhole" >&2
  session_json >&2 || true
  session_events >&2 || true
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
  curl -fsS "$base_url/internal/nodes" >&2 || true
  return 1
}

continuity_state_matches() {
  local expected_status="$1"
  local expected_video="$2"
  local expected_audio="$3"
  local payload
  payload="$("${compose[@]}" exec -T continuity sh -c 'cat /state/status.json 2>/dev/null || true' || true)"
  python3 -c '
import json,sys
d=json.load(sys.stdin)
expected=sys.argv[1:4]
actual=[d.get("session_status"), d.get("video_source"), d.get("actual_audio_mode")]
raise SystemExit(0 if actual == expected else 1)
' "$expected_status" "$expected_video" "$expected_audio" <<<"$payload" 2>/dev/null
}

wait_continuity_state() {
  local expected_status="$1"
  local expected_video="$2"
  local expected_audio="$3"
  local timeout="${4:-60}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if continuity_state_matches "$expected_status" "$expected_video" "$expected_audio"; then
      return 0
    fi
    sleep 0.5
  done
  # The state file can be updated on the timeout boundary between the last poll
  # and the deadline check. Perform one final observation before declaring failure.
  if continuity_state_matches "$expected_status" "$expected_video" "$expected_audio"; then
    return 0
  fi
  echo "Continuity did not become ${expected_status}/${expected_video}/${expected_audio}" >&2
  "${compose[@]}" exec -T continuity sh -c 'cat /state/status.json 2>/dev/null || true' >&2 || true
  return 1
}

assert_output_relay_online() {
  "${compose[@]}" exec -T continuity python3 - <<'PY'
import json
import urllib.request

with urllib.request.urlopen(
    "http://mediamtx:9997/v3/paths/list?itemsPerPage=100", timeout=3
) as response:
    payload = json.load(response)
relay = next(
    (item for item in payload.get("items", []) if item.get("name") == "output/relay"),
    None,
)
if relay is None or not relay.get("online"):
    raise SystemExit(f"output/relay is not online: {relay!r}")
codecs = {track.get("codec") for track in relay.get("tracks2", []) if isinstance(track, dict)}
if "H264" not in codecs or "MPEG-4 Audio" not in codecs:
    raise SystemExit(f"output/relay missing expected tracks; codecs={sorted(codecs)!r}")
print(f"output/relay online codecs={sorted(codecs)!r}")
PY
}

create_publisher_container() {
  local continuity_image mediamtx_id network_name
  continuity_image="$("${compose[@]}" images -q continuity | head -n1)"
  mediamtx_id="$("${compose[@]}" ps -q mediamtx)"
  [[ -n "$continuity_image" ]] || { echo "continuity image not found" >&2; return 1; }
  [[ -n "$mediamtx_id" ]] || { echo "mediamtx container not found" >&2; return 1; }
  network_name="$(docker inspect -f '{{range $name, $conf := .NetworkSettings.Networks}}{{$name}}{{"\n"}}{{end}}' "$mediamtx_id" | head -n1)"
  [[ -n "$network_name" ]] || { echo "compose network not found" >&2; return 1; }
  docker run -d --name "$publisher_name" --network "$network_name" "$continuity_image" sleep infinity >/dev/null
}

start_publisher() {
  docker exec -i \
    -e IRLIGHT_PUBLISH_USER="$ingest_username" \
    -e IRLIGHT_PUBLISH_PASS="$ingest_secret" \
    "$publisher_name" python3 - <<'PY' >/tmp/irlight-rtmp-netem-publisher.log 2>&1 &
from __future__ import annotations

import os
import time

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
    while True:
        message = bus.timed_pop_filtered(
            100 * Gst.MSECOND,
            Gst.MessageType.ERROR | Gst.MessageType.EOS,
        )
        if message is not None:
            if message.type == Gst.MessageType.ERROR:
                err, debug = message.parse_error()
                raise SystemExit(f"publisher error: {err}; {debug}")
            raise SystemExit("publisher reached EOS unexpectedly")
        time.sleep(0.05)
finally:
    pipeline.set_state(Gst.State.NULL)
PY
  publisher_pid=$!
}

assert_recovery_sequence() {
  local after_sequence="$1"
  local events_payload
  events_payload="$(session_events)"
  python3 -c '
import json,sys
after=int(sys.argv[1])
payload=json.load(sys.stdin)
all_events=payload if isinstance(payload, list) else payload.get("events", [])
events=[e for e in all_events if int(e.get("sequence", 0)) > after]
holding=next((e for e in events if e.get("type") == "session.holding"), None)
if holding is None:
    raise SystemExit(f"monitoring missing session.holding: {events!r}")
recovered=next((e for e in events if int(e.get("sequence", 0)) > int(holding.get("sequence", 0)) and e.get("type") == "session.recovered"), None)
if recovered is None:
    raise SystemExit(f"monitoring missing session.recovered after netem fault: {events!r}")
recovery_signal=next((
    e for e in events
    if int(e.get("sequence", 0)) > int(holding.get("sequence", 0))
    and e.get("type") in {"ingest.recovered", "ingest.reconnected"}
), None)
if recovery_signal is None:
    raise SystemExit(f"monitoring missing ingest recovery signal: {events!r}")
print(
    "monitoring sequence:",
    holding.get("reason_code"),
    "->", recovery_signal.get("type"), "-> session.recovered",
)
' "$after_sequence" <<<"$events_payload"
}

stage "start control plane"
"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" up -d --build control-ui
wait_http "$base_url/healthz" 60
stage "control plane ready"

curl -fsS --max-time 10 -X POST "$base_url/v1/auth/register" \
  -H 'Content-Type: application/json' \
  --data "{\"email\":\"$email\",\"password\":\"$password\",\"display_name\":\"RTMP Netem Smoke\"}" >/dev/null
login

session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
prepared="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/prepare" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  -H "Idempotency-Key: rtmp-netem-$session_id" \
  --data '{"environment":"dev"}')"
provider_server_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["provider_server_id"])' <<<"$prepared")"
if [[ -z "$provider_server_id" || "$provider_server_id" == "None" ]]; then
  echo "prepare did not allocate provider_server_id" >&2
  exit 1
fi
stage "session prepared"

export ASSIGNED_PROVIDER_SERVER_ID="$provider_server_id"
"${compose[@]}" up -d --build --no-deps node-agent
wait_assigned_node 45
stage "node assigned"

credential="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/ingest-credentials" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  --data '{"protocols":["rtmp"],"ttl_seconds":1800}')"
ingest_username="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])' <<<"$credential")"
ingest_secret="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["credential_secret"])' <<<"$credential")"

create_publisher_container
stage "dedicated publisher container ready"
start_publisher
stage "publisher started"
wait_session_status LIVE 60
wait_continuity_state LIVE LIVE LIVE 60
assert_output_relay_online
if ! kill -0 "$publisher_pid" 2>/dev/null; then
  echo "publisher exited before initial LIVE" >&2
  exit 1
fi
stage "initial LIVE and relay online"

baseline_sequence="$(latest_event_sequence)"
fault_started_at="$(date +%s)"
bash ./scripts/netem-container.sh apply "$publisher_name" --loss 100
netem_applied=1
stage "100% publisher egress packet loss applied"

holding_reason="$(wait_fault_holding_after "$baseline_sequence" 60)"
stage "session HOLDING reason=$holding_reason"
wait_continuity_state HOLDING STANDBY SILENT_FALLBACK 90
assert_output_relay_online
if ! kill -0 "$publisher_pid" 2>/dev/null; then
  echo "publisher process exited during network blackhole" >&2
  exit 1
fi
stage "standby relay continuous while publisher process remains alive"

elapsed=$(( $(date +%s) - fault_started_at ))
if (( elapsed < fault_seconds )); then
  sleep $(( fault_seconds - elapsed ))
fi
assert_output_relay_online
if ! kill -0 "$publisher_pid" 2>/dev/null; then
  echo "publisher process exited before netem clear" >&2
  exit 1
fi

bash ./scripts/netem-container.sh clear "$publisher_name"
netem_applied=0
stage "publisher network restored"
wait_session_status LIVE 90
wait_continuity_state LIVE LIVE LIVE 60
assert_output_relay_online
if ! kill -0 "$publisher_pid" 2>/dev/null; then
  echo "publisher process exited before recovered LIVE" >&2
  exit 1
fi
assert_recovery_sequence "$baseline_sequence"
stage "LIVE recovery and monitoring sequence verified"

echo "RTMP netem blackhole smoke passed (${fault_seconds}s requested fault, holding_reason=${holding_reason})"
