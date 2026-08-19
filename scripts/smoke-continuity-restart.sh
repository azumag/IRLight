#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp_dir="$(mktemp -d)"
override="$tmp_dir/continuity-restart.override.yml"
auth_cookie_jar="$tmp_dir/cookies.txt"
base_url="${BASE_URL:-http://127.0.0.1:8080}"
hls_url="${HLS_URL:-http://127.0.0.1:8888/output/relay/index.m3u8}"
compose=(docker compose -f "$repo_root/docker-compose.poc.yml")
test_compose=(docker compose -f "$repo_root/docker-compose.poc.yml" -f "$override")

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${test_compose[@]}" ps -a >&2 || true
    echo "--- continuity logs ---" >&2
    "${test_compose[@]}" logs --no-color --tail=180 continuity >&2 || true
    echo "--- mediamtx logs ---" >&2
    "${test_compose[@]}" logs --no-color --tail=120 mediamtx >&2 || true
    echo "--- status ---" >&2
    curl -fsS --max-time 3 "$base_url/api/status" >&2 || true
  fi
  "${test_compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT

wait_http() {
  local url="$1"
  local timeout="${2:-45}"
  local deadline=$((SECONDS + timeout))
  until curl -fsS --max-time 3 "$url" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "HTTP did not become ready: $url" >&2
      return 1
    fi
    sleep 1
  done
}

wait_node_registered() {
  local timeout="${1:-45}"
  local deadline=$((SECONDS + timeout))
  local payload=""
  while (( SECONDS < deadline )); do
    payload="$(curl -fsS --max-time 3 "$base_url/internal/nodes" 2>/dev/null || true)"
    if python3 -c 'import json,sys; value=json.load(sys.stdin); raise SystemExit(0 if value.get("nodes") else 1)' <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Node Agent did not register before restart smoke" >&2
  return 1
}

wait_runtime() {
  local expected_status="$1"
  local expected_video="$2"
  local expected_desired="$3"
  local expected_actual="$4"
  local timeout="${5:-45}"
  local deadline=$((SECONDS + timeout))
  local payload=""
  while (( SECONDS < deadline )); do
    payload="$(curl -fsS --max-time 3 "$base_url/api/status" 2>/dev/null || true)"
    if python3 -c '
import json, sys
value = json.load(sys.stdin)
runtime = value.get("runtime") or {}
control = value.get("control") or {}
expected = sys.argv[1:]
ok = (
    runtime.get("session_status") == expected[0]
    and runtime.get("video_source") == expected[1]
    and control.get("audio_mode") == expected[2]
    and runtime.get("actual_audio_mode") == expected[3]
)
raise SystemExit(0 if ok else 1)
' "$expected_status" "$expected_video" "$expected_desired" "$expected_actual" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  echo "runtime did not converge to $expected_status/$expected_video/$expected_desired/$expected_actual; last=$payload" >&2
  return 1
}

control_mode() {
  local mode="$1"
  local current version key
  current="$(curl -fsS --max-time 5 "$base_url/api/status")"
  version="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["control"]["version"])' <<<"$current")"
  key="restart-smoke-$(date +%s)-$RANDOM"
  curl -fsS --max-time 5 -X PUT "$base_url/api/audio" \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: $key" \
    --data "{\"mode\":\"$mode\",\"expected_version\":$version}" >/dev/null
}

wait_new_continuity_process() {
  local previous_started_at="$1"
  local timeout="${2:-30}"
  local deadline=$((SECONDS + timeout))
  local payload=""
  while (( SECONDS < deadline )); do
    payload="$(curl -fsS --max-time 3 "$base_url/api/status" 2>/dev/null || true)"
    if python3 -c '
import json, sys
value = json.load(sys.stdin)
runtime = value.get("runtime") or {}
control = value.get("control") or {}
started_at = runtime.get("started_at")
previous = float(sys.argv[1])
ok = (
    isinstance(started_at, (int, float))
    and float(started_at) > previous
    and control.get("audio_mode") == "MUTED"
)
raise SystemExit(0 if ok else 1)
' "$previous_started_at" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 0.25
  done
  echo "continuity process did not restart while preserving desired MUTED state; last=$payload" >&2
  return 1
}

register_ingest() {
  local email password login csrf session_id credential
  email="restart-$(date +%s)-$RANDOM@example.invalid"
  password="RestartSmoke123!"

  curl -fsS --max-time 10 -X POST "$base_url/v1/auth/register" \
    -H 'Content-Type: application/json' \
    --data "{\"email\":\"$email\",\"password\":\"$password\",\"display_name\":\"Restart Smoke\"}" >/dev/null

  login="$(curl -fsS --max-time 10 -c "$auth_cookie_jar" -X POST "$base_url/v1/auth/login" \
    -H 'Content-Type: application/json' \
    --data "{\"email\":\"$email\",\"password\":\"$password\"}")"
  csrf="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])' <<<"$login")"
  session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"

  curl -fsS --max-time 10 -b "$auth_cookie_jar" -X POST \
    "$base_url/v1/sessions/$session_id/prepare" \
    -H 'Content-Type: application/json' \
    -H "X-CSRF-Token: $csrf" \
    -H "Idempotency-Key: restart-prepare-$session_id" \
    --data '{"environment":"dev"}' >/dev/null

  credential="$(curl -fsS --max-time 10 -b "$auth_cookie_jar" -X POST \
    "$base_url/v1/sessions/$session_id/ingest-credentials" \
    -H 'Content-Type: application/json' \
    -H "X-CSRF-Token: $csrf" \
    --data '{"protocols":["rtmp"],"ttl_seconds":3600}')"

  RESTART_INGEST_USERNAME="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])' <<<"$credential")"
  RESTART_INGEST_SECRET="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["credential_secret"])' <<<"$credential")"
  export RESTART_INGEST_USERNAME RESTART_INGEST_SECRET
}

cat >"$override" <<'EOF'
services:
  restart-publisher:
    build:
      context: ./apps/continuity
      dockerfile: Dockerfile
    restart: "no"
    depends_on:
      - mediamtx
    environment:
      INGEST_USERNAME: ${RESTART_INGEST_USERNAME:-unset}
      INGEST_SECRET: ${RESTART_INGEST_SECRET:-unset}
    command:
      - /bin/sh
      - -c
      - |
        exec timeout --signal=INT --kill-after=5s 90s gst-launch-1.0 -q -e \
          flvmux name=mux streamable=true ! \
            rtmp2sink location="rtmp://mediamtx:1935/live/input?user=$${INGEST_USERNAME}&pass=$${INGEST_SECRET}" \
          videotestsrc is-live=true pattern=smpte ! \
            video/x-raw,width=1280,height=720,framerate=30/1,format=I420 ! \
            x264enc tune=zerolatency speed-preset=veryfast bitrate=1200 key-int-max=60 bframes=0 ! \
            video/x-h264,profile=main ! h264parse config-interval=-1 ! queue ! mux. \
          audiotestsrc is-live=true wave=sine freq=440 ! audioconvert ! audioresample ! \
            audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=128000 ! aacparse ! queue ! mux.
EOF

"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" up -d --build
wait_http "$base_url/api/status" 60
wait_node_registered 45
register_ingest

"${test_compose[@]}" up -d --build restart-publisher
wait_runtime LIVE LIVE LIVE LIVE 45

control_mode MUTED
wait_runtime LIVE LIVE MUTED MUTED 15

before_started_at="$(curl -fsS --max-time 5 "$base_url/api/status" | python3 -c 'import json,sys; print(json.load(sys.stdin)["runtime"]["started_at"])')"

# Restart only the Continuity Engine. The publisher runs in its own service so
# this test proves process recovery instead of accidentally terminating input.
"${test_compose[@]}" restart continuity >/dev/null
wait_new_continuity_process "$before_started_at" 30
wait_runtime LIVE LIVE MUTED MUTED 45
wait_http "$hls_url" 20

if ! "${test_compose[@]}" ps --status running --services | grep -qx restart-publisher; then
  echo "publisher did not survive continuity restart" >&2
  exit 1
fi

# The persisted desired state is the source of truth after restart. Returning to
# LIVE must be an explicit command, not a side effect of process recovery.
control_mode LIVE
wait_runtime LIVE LIVE LIVE LIVE 15

echo "IRLight continuity restart reconcile smoke test passed."
