#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -f docker-compose.poc.yml)
publisher_pid=""

show_logs_and_cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- docker compose ps ---" >&2
    "${compose[@]}" ps >&2 || true
    echo "--- docker compose logs ---" >&2
    "${compose[@]}" logs --no-color --tail=300 >&2 || true
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
    timeout --signal=INT ${duration}s gst-launch-1.0 -q -e \\
      flvmux name=mux streamable=true ! rtmpsink location=rtmp://mediamtx:1935/live/input \\
      videotestsrc is-live=true pattern=smpte ! \\
        video/x-raw,width=1280,height=720,framerate=30/1,format=I420 ! \\
        x264enc tune=zerolatency speed-preset=veryfast bitrate=1200 key-int-max=60 bframes=0 ! \\
        video/x-h264,profile=main ! h264parse config-interval=-1 ! queue ! mux. \\
      audiotestsrc is-live=true wave=sine freq=440 ! audioconvert ! audioresample ! \\
        audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=128000 ! aacparse ! queue ! mux.
  " >/tmp/irlight-publisher.log 2>&1 &
  publisher_pid=$!
}

"${compose[@]}" down --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" config >/dev/null
"${compose[@]}" up -d --build

./poc/scripts/wait-state.py \
  --timeout 90 \
  --session-status READY \
  --display-source STANDBY \
  --output-connected true \
  --audio-desired LIVE \
  --audio-actual MUTED

start_publisher 18
./poc/scripts/wait-state.py \
  --timeout 45 \
  --session-status LIVE \
  --display-source LIVE \
  --output-connected true \
  --audio-desired LIVE \
  --audio-actual LIVE

./poc/scripts/set-audio.py MUTED >/dev/null
./poc/scripts/wait-state.py \
  --timeout 10 \
  --session-status LIVE \
  --display-source LIVE \
  --output-connected true \
  --audio-desired MUTED \
  --audio-actual MUTED

./poc/scripts/set-audio.py LIVE >/dev/null
./poc/scripts/wait-state.py \
  --timeout 10 \
  --session-status LIVE \
  --display-source LIVE \
  --output-connected true \
  --audio-desired LIVE \
  --audio-actual LIVE

wait "$publisher_pid" || true
publisher_pid=""
./poc/scripts/wait-state.py \
  --timeout 15 \
  --session-status HOLDING \
  --display-source STANDBY \
  --output-connected true \
  --audio-desired LIVE \
  --audio-actual MUTED

start_publisher 10
./poc/scripts/wait-state.py \
  --timeout 30 \
  --session-status LIVE \
  --display-source LIVE \
  --output-connected true \
  --audio-desired LIVE \
  --audio-actual LIVE

wait "$publisher_pid" || true
publisher_pid=""
echo "IRLight Docker smoke test passed."
