#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp_dir="$(mktemp -d)"
override="$tmp_dir/egress-conflict.override.yml"
target_config="$tmp_dir/mediamtx-conflict.yml"
secret_file="$tmp_dir/egress_url"
stream_key="ci-conflict-$RANDOM"
path_name="conflict/$stream_key"
holder_pid=""
export EGRESS_SECRET_FILE="$secret_file"

cat >"$target_config" <<EOF
logLevel: info
rtmp: yes
rtmpAddress: :1935
paths:
  $path_name:
    source: publisher
    overridePublisher: false
EOF

cat >"$secret_file" <<EOF
rtmp://egress-conflict-target:1935/$path_name
EOF
chmod 600 "$secret_file"

cat >"$override" <<EOF
services:
  egress-conflict-target:
    image: bluenviron/mediamtx:1.20.0
    restart: "no"
    volumes:
      - $target_config:/mediamtx.yml:ro

  egress-conflict:
    build:
      context: ./apps/egress-gateway
      dockerfile: Dockerfile
    restart: "no"
    depends_on:
      - mediamtx
      - continuity
      - egress-conflict-target
    environment:
      EGRESS_INPUT_URI: rtsp://mediamtx:8554/output/relay
      EGRESS_URL_FILE: /run/irlight/secrets/egress_url
      EGRESS_STATUS_FILE: /state/egress.json
      # The target is intentionally on the isolated Compose network.
      EGRESS_ALLOW_PRIVATE_TARGETS: "1"
      EGRESS_CONNECT_TIMEOUT_SECONDS: "10"
      EGRESS_RETRY_INITIAL_SECONDS: "5"
      EGRESS_RETRY_MAX_SECONDS: "5"
      EGRESS_RETRY_MULTIPLIER: "2"
      EGRESS_RETRY_JITTER_RATIO: "0"
      EGRESS_MAX_ATTEMPTS: "0"
      EGRESS_MAX_RETRY_SECONDS: "0"
    volumes:
      - irlight-state:/state
      - $secret_file:/run/irlight/secrets/egress_url:ro
EOF

compose=(docker compose -f "$repo_root/docker-compose.poc.yml" -f "$override")

cleanup() {
  status=$?
  if [[ -n "$holder_pid" ]]; then
    kill "$holder_pid" 2>/dev/null || true
    wait "$holder_pid" 2>/dev/null || true
  fi
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps -a >&2 || true
    echo "--- holder output ---" >&2
    cat "$tmp_dir/holder.log" >&2 2>/dev/null || true
    echo "--- egress logs ---" >&2
    "${compose[@]}" logs --no-color --tail=160 egress-conflict >&2 || true
    echo "--- target logs ---" >&2
    "${compose[@]}" logs --no-color --tail=160 egress-conflict-target >&2 || true
  fi
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT

read_status() {
  "${compose[@]}" exec -T continuity cat /state/egress.json 2>/dev/null || true
}

wait_status_reason() {
  local expected_status="$1"
  local expected_reason="$2"
  local timeout="${3:-45}"
  local deadline=$((SECONDS + timeout))
  local payload=""
  while (( SECONDS < deadline )); do
    payload="$(read_status)"
    if python3 -c '
import json,sys
try:
    value=json.loads(sys.argv[1])
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if value.get("status") == sys.argv[2] and value.get("reason_code") == sys.argv[3] else 1)
' "$payload" "$expected_status" "$expected_reason" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "egress status did not become $expected_status/$expected_reason; last=$payload" >&2
  return 1
}

"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" up -d --build mediamtx continuity egress-conflict-target
sleep 2

# Hold the target path with a first publisher. MediaMTX is configured with
# overridePublisher=false, so a second publisher must be rejected rather than
# silently evicting this one.
"${compose[@]}" exec -T continuity sh -c "
  timeout --signal=INT --kill-after=5s 90s gst-launch-1.0 -q -e \
    flvmux name=mux streamable=true ! \
      rtmp2sink location='rtmp://egress-conflict-target:1935/$path_name' \
    videotestsrc is-live=true pattern=black ! \
      video/x-raw,width=640,height=360,framerate=15/1,format=I420 ! \
      x264enc tune=zerolatency speed-preset=veryfast bitrate=600 key-int-max=30 bframes=0 ! \
      video/x-h264,profile=main ! h264parse config-interval=-1 ! queue ! mux. \
    audiotestsrc is-live=true wave=silence ! audioconvert ! audioresample ! \
      audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=96000 ! aacparse ! queue ! mux.
" >"$tmp_dir/holder.log" 2>&1 &
holder_pid=$!

for _ in $(seq 1 12); do
  if ! kill -0 "$holder_pid" 2>/dev/null; then
    echo "first publisher exited before conflict test" >&2
    cat "$tmp_dir/holder.log" >&2 || true
    exit 1
  fi
  if "${compose[@]}" logs --no-color egress-conflict-target 2>/dev/null | grep -Fq "is publishing to path '$path_name'"; then
    break
  fi
  sleep 1
done
if ! "${compose[@]}" logs --no-color egress-conflict-target 2>/dev/null | grep -Fq "is publishing to path '$path_name'"; then
  echo "first publisher did not become active on conflict target" >&2
  exit 1
fi

"${compose[@]}" up -d egress-conflict
wait_status_reason AUTH_FAILED PUBLISH_CONFLICT 45

# The conflict is terminal: the Gateway must exit instead of entering a retry
# loop, and the original publisher must remain alive.
for _ in $(seq 1 10); do
  if ! "${compose[@]}" ps --status running --services | grep -qx egress-conflict; then
    break
  fi
  sleep 1
done
if "${compose[@]}" ps --status running --services | grep -qx egress-conflict; then
  echo "publish conflict left egress gateway running/retrying" >&2
  exit 1
fi
if ! kill -0 "$holder_pid" 2>/dev/null; then
  echo "publish conflict evicted the original publisher" >&2
  exit 1
fi

status_payload="$(read_status)"
if grep -Fq "$stream_key" <<<"$status_payload"; then
  echo "egress conflict status leaked stream key" >&2
  exit 1
fi
if "${compose[@]}" logs --no-color egress-conflict | grep -Fq "$stream_key"; then
  echo "egress conflict logs leaked stream key" >&2
  exit 1
fi
if ! "${compose[@]}" ps --status running --services | grep -qx continuity; then
  echo "continuity stopped during publish conflict" >&2
  exit 1
fi

echo "IRLight egress publish-conflict smoke passed."
