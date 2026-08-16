#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${COMPOSE_OVERRIDE:-}" ]]; then
  compose=(docker compose -f docker-compose.poc.yml -f "$COMPOSE_OVERRIDE")
else
  compose=(docker compose -f docker-compose.poc.yml)
fi
base_url="${BASE_URL:-http://127.0.0.1:8080}"
hls_url="${HLS_URL:-http://127.0.0.1:8888/output/relay/index.m3u8}"
publisher_pid=""
current_stage="bootstrap"

annotation_escape() {
  local value="$1"
  value="${value//'%'/'%25'}"
  value="${value//$'\r'/'%0D'}"
  value="${value//$'\n'/'%0A'}"
  printf '%s' "$value"
}

compact_diagnostics() {
  local status ps continuity control mediamtx publisher
  status="$(curl -fsS --max-time 3 "$base_url/api/status" 2>&1 || true)"
  ps="$("${compose[@]}" ps --format json 2>&1 | tail -c 2000 || true)"
  continuity="$("${compose[@]}" logs --no-color --tail=50 continuity 2>&1 | tail -c 5000 || true)"
  control="$("${compose[@]}" logs --no-color --tail=50 control-ui 2>&1 | tail -c 3000 || true)"
  mediamtx="$("${compose[@]}" logs --no-color --tail=50 mediamtx 2>&1 | tail -c 3000 || true)"
  publisher="$(tail -c 2000 /tmp/irlight-publisher.log 2>/dev/null || true)"
  printf 'stage=%s\nstatus=%s\nps=%s\ncontinuity=%s\ncontrol=%s\nmediamtx=%s\npublisher=%s' \
    "$current_stage" "$status" "$ps" "$continuity" "$control" "$mediamtx" "$publisher"
}

show_logs_and_cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- docker compose ps ---" >&2
    "${compose[@]}" ps >&2 || true
    echo "--- docker compose logs ---" >&2
    "${compose[@]}" logs --no-color --tail=300 >&2 || true
    diagnostics="$(compact_diagnostics)"
    echo "::error title=IRLight docker smoke failure::$(annotation_escape "$diagnostics")"
  fi
  if [[ -n "$publisher_pid" ]]; then
    kill "$publisher_pid" 2>/dev/null || true
    wait "$publisher_pid" 2>/dev/null || true
  fi
  "${compose[@]}" down --remove-orphans >/dev/null 2>&1 || true
  exit "$status"
}
trap show_logs_and_cleanup EXIT

start_publisher() {
  duration="${1:-18}"
  "${compose[@]}" exec -T continuity sh -c "
    timeout --signal=INT ${duration}s gst-launch-1.0 -q -e \
      flvmux name=mux streamable=true ! rtmp2sink location=rtmp://mediamtx:1935/live/input \
      videotestsrc is-live=true pattern=smpte ! \
        video/x-raw,width=1280,height=720,framerate=30/1,format=I420 ! \
        x264enc tune=zerolatency speed-preset=veryfast bitrate=1200 key-int-max=60 bframes=0 ! \
        video/x-h264,profile=main ! h264parse config-interval=-1 ! queue ! mux. \
      audiotestsrc is-live=true wave=sine freq=440 ! audioconvert ! audioresample ! \
        audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=128000 ! aacparse ! queue ! mux.
  " >/tmp/irlight-publisher.log 2>&1 &
  publisher_pid=$!
}

wait_http() {
  local url="$1"
  local timeout="${2:-30}"
  local deadline=$((SECONDS + timeout))
  until curl -fsS --max-time 3 "$url" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "HTTP が応答しませんでした: $url" >&2
      return 1
    fi
    sleep 1
  done
}

control_mode() {
  local mode="$1"
  local current version key
  current="$(curl -fsS --max-time 5 "$base_url/api/status")"
  version="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["control"]["version"])' <<<"$current")"
  key="smoke-$(date +%s)-$RANDOM"
  curl -fsS --max-time 5 -X PUT "$base_url/api/audio" \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: $key" \
    --data "{\"mode\":\"$mode\",\"expected_version\":$version}" >/dev/null
}

wait_status() {
  ./scripts/wait-status.py --base-url "$base_url" "$@"
}

current_stage="compose-up"
"${compose[@]}" down --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" config >/dev/null
"${compose[@]}" up -d --build

current_stage="initial-holding"
wait_status \
  --timeout 90 \
  --session-status HOLDING \
  --video-source STANDBY \
  --audio-desired LIVE \
  --audio-actual SILENT_FALLBACK
wait_http "$hls_url" 30

current_stage="first-live"
start_publisher 20
wait_status \
  --timeout 45 \
  --session-status LIVE \
  --video-source LIVE \
  --audio-desired LIVE \
  --audio-actual LIVE

current_stage="mute"
control_mode MUTED
wait_status \
  --timeout 10 \
  --session-status LIVE \
  --video-source LIVE \
  --audio-desired MUTED \
  --audio-actual MUTED

current_stage="unmute"
control_mode LIVE
wait_status \
  --timeout 10 \
  --session-status LIVE \
  --video-source LIVE \
  --audio-desired LIVE \
  --audio-actual LIVE

current_stage="return-to-holding"
wait "$publisher_pid" || true
publisher_pid=""
wait_status \
  --timeout 20 \
  --session-status HOLDING \
  --video-source STANDBY \
  --audio-desired LIVE \
  --audio-actual SILENT_FALLBACK

current_stage="second-live"
start_publisher 15
wait_status \
  --timeout 30 \
  --session-status LIVE \
  --video-source LIVE \
  --audio-desired LIVE \
  --audio-actual LIVE
wait "$publisher_pid" || true
publisher_pid=""

current_stage="mute-persistence"
start_publisher 30
wait_status \
  --timeout 30 \
  --session-status LIVE \
  --video-source LIVE \
  --audio-desired LIVE \
  --audio-actual LIVE
control_mode MUTED
wait_status \
  --timeout 10 \
  --session-status LIVE \
  --video-source LIVE \
  --audio-desired MUTED \
  --audio-actual MUTED
wait "$publisher_pid" || true
publisher_pid=""
wait_status \
  --timeout 20 \
  --session-status HOLDING \
  --video-source STANDBY \
  --audio-desired MUTED \
  --audio-actual MUTED

current_stage="muted-recovery"
start_publisher 15
wait_status \
  --timeout 30 \
  --session-status LIVE \
  --video-source LIVE \
  --audio-desired MUTED \
  --audio-actual MUTED
control_mode LIVE
wait_status \
  --timeout 10 \
  --session-status LIVE \
  --video-source LIVE \
  --audio-desired LIVE \
  --audio-actual LIVE
wait "$publisher_pid" || true
publisher_pid=""
wait_http "$hls_url" 15

current_stage="complete"
echo "IRLight Docker smoke test passed."
