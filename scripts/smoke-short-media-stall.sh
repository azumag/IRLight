#!/usr/bin/env bash
set -euo pipefail

tmp_dir="$(mktemp -d)"
override="$tmp_dir/short-media-stall.override.yml"
cookie_jar="$tmp_dir/cookies.txt"
base_url="${BASE_URL:-http://127.0.0.1:8080}"
stall_seconds="${MEDIA_STALL_SECONDS:-10}"
publisher_pid=""
email="short-media-stall-$(date +%s)-$RANDOM@example.invalid"
password='SmokePassword123!'

case "$stall_seconds" in
  ''|*[!0-9]*)
    echo "MEDIA_STALL_SECONDS must be a positive integer" >&2
    exit 2
    ;;
esac
if (( stall_seconds < 1 )); then
  echo "MEDIA_STALL_SECONDS must be at least 1" >&2
  exit 2
fi

cat >"$override" <<'YAML'
services:
  control-ui:
    environment:
      NODE_BOOTSTRAP_TOKENS: short-media-stall-node-token
      NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT: "1"
      RECOVERY_STABLE_SECONDS: "3"
  node-agent:
    environment:
      NODE_BOOTSTRAP_TOKEN: short-media-stall-node-token
      NODE_PROVIDER_SERVER_ID: ${ASSIGNED_PROVIDER_SERVER_ID:-unassigned-provider}
      NODE_BOOT_ID: short-media-stall-boot
      NODE_HEARTBEAT_INTERVAL: "2"
      NODE_INGEST_SAMPLE_SECONDS: "2"
      NODE_INGEST_SAMPLE_TIMEOUT_MARGIN_SECONDS: "2"
YAML

compose=(docker compose -f docker-compose.poc.yml -f "$override")

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps >&2 || true
    echo "--- continuity status ---" >&2
    "${compose[@]}" exec -T continuity sh -c 'cat /state/status.json 2>/dev/null || true' >&2 || true
    echo "--- node-agent logs ---" >&2
    "${compose[@]}" logs --no-color --tail=180 node-agent >&2 || true
    echo "--- continuity logs ---" >&2
    "${compose[@]}" logs --no-color --tail=160 continuity >&2 || true
    echo "--- control logs ---" >&2
    "${compose[@]}" logs --no-color --tail=160 control-ui >&2 || true
    echo "--- mediamtx logs ---" >&2
    "${compose[@]}" logs --no-color --tail=120 mediamtx >&2 || true
  fi
  "${compose[@]}" exec -T continuity sh -c \
    'rm -f /tmp/irlight-short-stall-video /tmp/irlight-short-stall-audio /tmp/irlight-stop-short-stall-publisher' \
    >/dev/null 2>&1 || true
  if [[ -n "$publisher_pid" ]]; then
    kill "$publisher_pid" 2>/dev/null || true
    wait "$publisher_pid" 2>/dev/null || true
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

session_events() {
  curl -fsS --max-time 5 -b "$cookie_jar" "$base_url/v1/sessions/$session_id/events"
}

latest_event_seq() {
  session_events | python3 -c '
import json,sys
events=json.load(sys.stdin).get("events", [])
print(max((int(e.get("seq", 0)) for e in events), default=0))
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

wait_stall_holding_after() {
  local after_seq="$1"
  local timeout="${2:-45}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    status_payload="$(session_json 2>/dev/null || true)"
    events_payload="$(session_events 2>/dev/null || true)"
    if python3 -c '
import json,sys
session=json.loads(sys.argv[1])
after=int(sys.argv[2])
events=json.load(sys.stdin).get("events", [])
reasons={"VIDEO_TIMEOUT", "AUDIO_TIMEOUT"}
ok=session.get("status") == "HOLDING" and any(
    int(e.get("seq", 0)) > after
    and e.get("type") == "session.holding"
    and e.get("reason_code") in reasons
    for e in events
)
raise SystemExit(0 if ok else 1)
' "$status_payload" "$after_seq" <<<"$events_payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Session did not enter media-timeout HOLDING after seq $after_seq" >&2
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

wait_continuity_state() {
  local expected_status="$1"
  local expected_video="$2"
  local expected_audio="$3"
  local timeout="${4:-45}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    payload="$("${compose[@]}" exec -T continuity sh -c 'cat /state/status.json 2>/dev/null || true' || true)"
    if python3 -c '
import json,sys
d=json.load(sys.stdin)
expected=sys.argv[1:4]
actual=[d.get("session_status"), d.get("video_source"), d.get("actual_audio_mode")]
raise SystemExit(0 if actual == expected else 1)
' "$expected_status" "$expected_video" "$expected_audio" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
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
codecs = {track.get("codec") for track in relay.get("tracks2", [])}
if "H264" not in codecs or "MPEG4Audio" not in codecs and "MPEG-4 Audio" not in codecs:
    raise SystemExit(f"output/relay missing expected tracks: {relay!r}")
PY
}

set_media_stall() {
  local enabled="$1"
  if [[ "$enabled" == "1" ]]; then
    "${compose[@]}" exec -T continuity sh -c \
      'touch /tmp/irlight-short-stall-video /tmp/irlight-short-stall-audio'
  else
    "${compose[@]}" exec -T continuity sh -c \
      'rm -f /tmp/irlight-short-stall-video /tmp/irlight-short-stall-audio'
  fi
}

start_publisher() {
  "${compose[@]}" exec -T continuity sh -c \
    'rm -f /tmp/irlight-short-stall-video /tmp/irlight-short-stall-audio /tmp/irlight-stop-short-stall-publisher'
  "${compose[@]}" exec -T \
    -e IRLIGHT_PUBLISH_USER="$ingest_username" \
    -e IRLIGHT_PUBLISH_PASS="$ingest_secret" \
    continuity python3 - <<'PY' >/tmp/irlight-short-media-stall-publisher.log 2>&1 &
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
      video/x-h264,profile=main ! h264parse config-interval=-1 !
      valve name=video_gate drop=false drop-mode=transform-to-gap ! queue ! mux.
    audiotestsrc is-live=true wave=sine freq=440 ! audioconvert ! audioresample !
      audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=128000 ! aacparse !
      valve name=audio_gate drop=false drop-mode=transform-to-gap ! queue ! mux.'''
)
video_gate = pipeline.get_by_name("video_gate")
audio_gate = pipeline.get_by_name("audio_gate")
assert video_gate is not None and audio_gate is not None
bus = pipeline.get_bus()
if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
    raise SystemExit("publisher failed to start")
try:
    while not Path("/tmp/irlight-stop-short-stall-publisher").exists():
        video_gate.set_property(
            "drop", Path("/tmp/irlight-short-stall-video").exists()
        )
        audio_gate.set_property(
            "drop", Path("/tmp/irlight-short-stall-audio").exists()
        )
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

assert_monitoring_sequence() {
  local after_seq="$1"
  local events_payload
  events_payload="$(session_events)"
  python3 -c '
import json,sys
after=int(sys.argv[1])
events=[e for e in json.load(sys.stdin).get("events", []) if int(e.get("seq", 0)) > after]
holding=next((e for e in events if e.get("type") == "session.holding" and e.get("reason_code") in {"VIDEO_TIMEOUT", "AUDIO_TIMEOUT"}), None)
if holding is None:
    raise SystemExit(f"monitoring missing media-timeout session.holding: {events!r}")
recovered=next((e for e in events if int(e.get("seq", 0)) > int(holding.get("seq", 0)) and e.get("type") == "session.recovered"), None)
if recovered is None:
    raise SystemExit(f"monitoring missing session.recovered after stall: {events!r}")
if any(e.get("type") == "ingest.disconnected" for e in events if int(e.get("seq", 0)) <= int(recovered.get("seq", 0))):
    raise SystemExit(f"short media stall unexpectedly disconnected RTMP ingest: {events!r}")
ingest_recovered=next((e for e in events if e.get("type") == "ingest.recovered"), None)
if ingest_recovered is None:
    raise SystemExit(f"monitoring missing ingest.recovered for same connection: {events!r}")
print(
    "monitoring sequence:",
    holding.get("reason_code"),
    "-> ingest.recovered -> session.recovered",
)
' "$after_seq" <<<"$events_payload"
}

"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" up -d --build control-ui
wait_http "$base_url/healthz" 60

curl -fsS --max-time 10 -X POST "$base_url/v1/auth/register" \
  -H 'Content-Type: application/json' \
  --data "{\"email\":\"$email\",\"password\":\"$password\",\"display_name\":\"Short Media Stall Smoke\"}" >/dev/null
login

session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
prepared="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/prepare" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  -H "Idempotency-Key: short-media-stall-$session_id" \
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
wait_continuity_state LIVE LIVE LIVE 60
assert_output_relay_online
if ! kill -0 "$publisher_pid" 2>/dev/null; then
  echo "publisher exited before initial LIVE" >&2
  exit 1
fi

baseline_seq="$(latest_event_seq)"
stall_started_at="$(date +%s)"
set_media_stall 1
wait_stall_holding_after "$baseline_seq" 45
wait_continuity_state HOLDING STANDBY SILENT_FALLBACK 45
assert_output_relay_online
if ! kill -0 "$publisher_pid" 2>/dev/null; then
  echo "publisher exited during full-media stall" >&2
  exit 1
fi

elapsed=$(( $(date +%s) - stall_started_at ))
if (( elapsed < stall_seconds )); then
  sleep $(( stall_seconds - elapsed ))
fi
# Verify the relay is still online at the end of the requested loss window,
# not just at the moment HOLDING was first detected.
assert_output_relay_online

set_media_stall 0
wait_session_status LIVE 60
wait_continuity_state LIVE LIVE LIVE 60
assert_output_relay_online
if ! kill -0 "$publisher_pid" 2>/dev/null; then
  echo "publisher exited before recovered LIVE" >&2
  exit 1
fi
assert_monitoring_sequence "$baseline_seq"

echo "short full-media stall smoke passed (${stall_seconds}s stall, standby output continuous)"
