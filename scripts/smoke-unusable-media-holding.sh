#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/node-admin.sh"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
umask 077
tmp_dir="$(mktemp -d)"
override="$tmp_dir/unusable-media.override.yml"
cookie_jar="$tmp_dir/cookies.txt"
publisher_log="$tmp_dir/publisher.log"
invalid_publisher_log="$tmp_dir/invalid-publisher.log"
base_url="${BASE_URL:-http://127.0.0.1:8080}"
publisher_pid=""
invalid_pid=""
email="unusable-media-$(date +%s)-$RANDOM@example.invalid"
password='SmokePassword123!'

cat >"$override" <<'YAML'
services:
  control-ui:
    environment:
      NODE_BOOTSTRAP_TOKENS: unusable-media-node-token
      NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT: "1"
  node-agent:
    environment:
      NODE_BOOTSTRAP_TOKEN: unusable-media-node-token
      NODE_PROVIDER_SERVER_ID: ${ASSIGNED_PROVIDER_SERVER_ID:-unassigned-provider}
      NODE_BOOT_ID: unusable-media-boot
      # Leave a deterministic handoff window between observations while keeping
      # the Node-local auth proxy alive for the replacement publisher.
      NODE_HEARTBEAT_INTERVAL: "8"
      NODE_INGEST_SAMPLE_SECONDS: "2"
      NODE_INGEST_SAMPLE_TIMEOUT_MARGIN_SECONDS: "2"
YAML

smoke_project="irlight-unusable-media-holding-smoke-$$-$RANDOM"
compose=(docker compose -p "$smoke_project" -f "$repo_root/docker-compose.poc.yml" -f "$override")

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps >&2 || true
    echo "--- node-agent logs ---" >&2
    "${compose[@]}" logs --no-color --tail=180 node-agent >&2 || true
    echo "--- control logs ---" >&2
    "${compose[@]}" logs --no-color --tail=140 control-ui >&2 || true
    echo "--- mediamtx logs ---" >&2
    "${compose[@]}" logs --no-color --tail=120 mediamtx >&2 || true
    echo "--- publisher log ---" >&2
    cat "$publisher_log" >&2 2>/dev/null || true
    echo "--- invalid publisher log ---" >&2
    cat "$invalid_publisher_log" >&2 2>/dev/null || true
  fi
  for pid in "$publisher_pid" "$invalid_pid"; do
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
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
  local timeout="${2:-45}"
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

wait_holding_reason() {
  local expected="$1"
  local timeout="${2:-45}"
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

wait_mediamtx_resolution() {
  local width="$1"
  local height="$2"
  local timeout="${3:-20}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if "${compose[@]}" exec -T continuity python3 -c '
import json,sys,urllib.request
width,height=int(sys.argv[1]),int(sys.argv[2])
with urllib.request.urlopen("http://mediamtx:9997/v3/paths/list?itemsPerPage=100", timeout=2) as response:
    payload=json.load(response)
for path in payload.get("items", []):
    if path.get("name") != "live/input" or not path.get("online"):
        continue
    for track in path.get("tracks2", []):
        props=track.get("codecProps") or {}
        if track.get("codec") == "H264" and props.get("width") == width and props.get("height") == height:
            raise SystemExit(0)
raise SystemExit(1)
' "$width" "$height" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "MediaMTX did not observe ${width}x${height} publisher" >&2
  return 1
}

set_gate() {
  local kind="$1"
  local enabled="$2"
  local marker="/tmp/irlight-drop-$kind"
  if [[ "$enabled" == "1" ]]; then
    "${compose[@]}" exec -T continuity sh -c "touch '$marker'"
  else
    "${compose[@]}" exec -T continuity sh -c "rm -f '$marker'"
  fi
}

stop_controllable_publisher() {
  "${compose[@]}" exec -T continuity sh -c 'touch /tmp/irlight-stop-publisher'
  if [[ -n "$publisher_pid" ]]; then
    wait "$publisher_pid" 2>/dev/null || true
    publisher_pid=""
  fi
  "${compose[@]}" exec -T continuity sh -c 'rm -f /tmp/irlight-stop-publisher'
}

start_controllable_publisher() {
  "${compose[@]}" exec -T continuity sh -c 'rm -f /tmp/irlight-stop-publisher /tmp/irlight-drop-video /tmp/irlight-drop-audio'
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
description = f"""
flvmux name=mux streamable=true ! rtmp2sink location=\"{url}\"
videotestsrc is-live=true pattern=smpte !
  video/x-raw,width=1280,height=720,framerate=30/1,format=I420 !
  x264enc tune=zerolatency speed-preset=veryfast bitrate=1200 key-int-max=60 bframes=0 !
  video/x-h264,profile=main ! h264parse config-interval=-1 !
  valve name=video_gate drop=false drop-mode=transform-to-gap ! queue ! mux.
audiotestsrc is-live=true wave=sine freq=440 ! audioconvert ! audioresample !
  audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=128000 ! aacparse !
  valve name=audio_gate drop=false drop-mode=transform-to-gap ! queue ! mux.
"""
pipeline = Gst.parse_launch(description)
video_gate = pipeline.get_by_name("video_gate")
audio_gate = pipeline.get_by_name("audio_gate")
assert video_gate is not None and audio_gate is not None
bus = pipeline.get_bus()
if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
    raise SystemExit("publisher failed to start")
try:
    while not Path("/tmp/irlight-stop-publisher").exists():
        # Transform dropped buffers into GAP events so flvmux can keep forwarding
        # the healthy sibling track without producing frames for the gated track.
        video_gate.set_property("drop", Path("/tmp/irlight-drop-video").exists())
        audio_gate.set_property("drop", Path("/tmp/irlight-drop-audio").exists())
        message = bus.timed_pop_filtered(
            100 * Gst.MSECOND,
            Gst.MessageType.ERROR | Gst.MessageType.EOS,
        )
        if message is not None:
            break
        time.sleep(0.05)
finally:
    pipeline.set_state(Gst.State.NULL)
PY
  publisher_pid=$!
}

start_invalid_resolution_publisher() {
  # shellcheck disable=SC2016
  "${compose[@]}" exec -T \
    -e IRLIGHT_PUBLISH_USER="$ingest_username" \
    -e IRLIGHT_PUBLISH_PASS="$ingest_secret" \
    continuity sh -c '
      timeout --signal=INT --kill-after=3s 20s gst-launch-1.0 -q -e \
        flvmux name=mux streamable=true ! \
          rtmp2sink location="rtmp://mediamtx:1935/live/input?user=${IRLIGHT_PUBLISH_USER}&pass=${IRLIGHT_PUBLISH_PASS}" \
        videotestsrc is-live=true pattern=ball ! \
          video/x-raw,width=640,height=360,framerate=30/1,format=I420 ! \
          x264enc tune=zerolatency speed-preset=veryfast bitrate=800 key-int-max=30 bframes=0 ! \
          video/x-h264,profile=main ! h264parse config-interval=-1 ! queue ! mux. \
        audiotestsrc is-live=true wave=sine freq=880 ! audioconvert ! audioresample ! \
          audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=128000 ! aacparse ! queue ! mux.
    ' >"$invalid_publisher_log" 2>&1 &
  invalid_pid=$!
}

"${compose[@]}" config >/dev/null
"${compose[@]}" up -d --build control-ui
wait_http "$base_url/healthz" 60

curl -fsS --max-time 10 -X POST "$base_url/v1/auth/register" \
  -H 'Content-Type: application/json' \
  --data "{\"email\":\"$email\",\"password\":\"$password\",\"display_name\":\"Unusable Media Smoke\"}" >/dev/null
login

session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
prepared="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/prepare" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  -H "Idempotency-Key: unusable-media-$session_id" \
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
  --data '{"protocols":["rtmp"],"ttl_seconds":3600}')"
ingest_username="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])' <<<"$credential")"
ingest_secret="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["credential_secret"])' <<<"$credential")"

start_controllable_publisher
wait_session_status LIVE 50
if ! kill -0 "$publisher_pid" 2>/dev/null; then
  echo "controllable publisher exited before LIVE" >&2
  exit 1
fi

# Stop only video buffers while RTMP and audio continue. The quality sampler
# must classify VIDEO_TIMEOUT and the formal Session must enter HOLDING.
set_gate video 1
wait_holding_reason VIDEO_TIMEOUT 45
if ! kill -0 "$publisher_pid" 2>/dev/null; then
  echo "video-timeout publisher was disconnected unexpectedly" >&2
  exit 1
fi
set_gate video 0
wait_session_status LIVE 50

# Repeat for audio-only loss without dropping the RTMP publisher connection.
set_gate audio 1
wait_holding_reason AUDIO_TIMEOUT 45
if ! kill -0 "$publisher_pid" 2>/dev/null; then
  echo "audio-timeout publisher was disconnected unexpectedly" >&2
  exit 1
fi
set_gate audio 0
wait_session_status LIVE 50

# Model a fast publisher reconnect whose media format changes before the next
# Node observation. Keep the Node process alive so MediaMTX still authenticates
# the replacement publisher through the node-bound proxy.
stop_controllable_publisher
start_invalid_resolution_publisher
wait_mediamtx_resolution 640 360 20
wait_holding_reason FORMAT_CHANGED 45

final_events="$(session_events)"
python3 -c '
import json,sys
events=json.load(sys.stdin).get("events", [])
holding=[(e.get("reason_code"), e.get("payload", {}).get("reasons", [])) for e in events if e.get("type") == "session.holding"]
assert any(reason == "VIDEO_TIMEOUT" for reason, _ in holding), holding
assert any(reason == "AUDIO_TIMEOUT" for reason, _ in holding), holding
assert any(reason == "FORMAT_CHANGED" and "RESOLUTION_UNSUPPORTED" in reasons for reason, reasons in holding), holding
' <<<"$final_events"
if grep -Fq "$ingest_secret" <<<"$final_events"; then
  echo "raw ingest secret leaked into Session events" >&2
  exit 1
fi

echo "IRLight unusable-media HOLDING smoke passed."
