#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/node-admin.sh"

smoke_project="${IRLIGHT_SMOKE_PROJECT:-irlight-poc-smoke-$$-$RANDOM}"
if [[ -n "${COMPOSE_OVERRIDE:-}" ]]; then
  compose=(docker compose -p "$smoke_project" -f docker-compose.poc.yml -f "$COMPOSE_OVERRIDE")
else
  compose=(docker compose -p "$smoke_project" -f docker-compose.poc.yml)
fi
base_url="${BASE_URL:-http://127.0.0.1:8080}"
hls_url="${HLS_URL:-http://127.0.0.1:8888/output/relay/index.m3u8}"
publisher_pid=""
auth_cookie_jar="/tmp/irlight-smoke-cookies.txt"
ingest_username=""
ingest_secret=""
ingest_session_id=""
current_stage="bootstrap"

annotation_escape() {
  local value="$1"
  value="${value//'%'/'%25'}"
  value="${value//$'\r'/'%0D'}"
  value="${value//$'\n'/'%0A'}"
  printf '%s' "$value"
}

compact_diagnostics() {
  local status ps continuity control mediamtx node_agent publisher
  status="$(curl -fsS --max-time 3 "$base_url/api/status" 2>&1 || true)"
  ps="$("${compose[@]}" ps --format json 2>&1 | tail -c 2000 || true)"
  continuity="$("${compose[@]}" logs --no-color --tail=50 continuity 2>&1 | tail -c 5000 || true)"
  control="$("${compose[@]}" logs --no-color --tail=50 control-ui 2>&1 | tail -c 3000 || true)"
  mediamtx="$("${compose[@]}" logs --no-color --tail=50 mediamtx 2>&1 | tail -c 3000 || true)"
  node_agent="$("${compose[@]}" logs --no-color --tail=50 node-agent 2>&1 | tail -c 3000 || true)"
  publisher="$(tail -c 2000 /tmp/irlight-publisher.log 2>/dev/null || true)"
  printf 'stage=%s\nstatus=%s\nps=%s\ncontinuity=%s\ncontrol=%s\nmediamtx=%s\nnode_agent=%s\npublisher=%s' \
    "$current_stage" "$status" "$ps" "$continuity" "$control" "$mediamtx" "$node_agent" "$publisher"
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
  rm -f "$auth_cookie_jar"
  "${compose[@]}" down --rmi local --volumes --remove-orphans >/dev/null 2>&1 || true
  exit "$status"
}
trap show_logs_and_cleanup EXIT

start_publisher() {
  duration="${1:-18}"
  width="${2:-1280}"
  height="${3:-720}"
  if [[ -z "$ingest_username" || -z "$ingest_secret" ]]; then
    echo "ingest credential is not initialized" >&2
    return 1
  fi
  "${compose[@]}" exec -T continuity sh -c "
    timeout --signal=INT ${duration}s gst-launch-1.0 -q -e \
      flvmux name=mux streamable=true ! rtmp2sink location='rtmp://mediamtx:1935/live/input?user=${ingest_username}&pass=${ingest_secret}' \
      videotestsrc is-live=true pattern=smpte ! \
        video/x-raw,width=${width},height=${height},framerate=30/1,format=I420 ! \
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

wait_node_registered() {
  local timeout="${1:-30}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    payload="$(node_admin_curl -fsS --max-time 3 "$base_url/internal/nodes" 2>/dev/null || true)"
    if python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("nodes") else 1)' <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Node Agent がControl Planeへ登録されませんでした" >&2
  return 1
}

wait_node_ingest_status() {
  local expected="$1"
  local timeout="${2:-20}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    payload="$(node_admin_curl -fsS --max-time 3 "$base_url/internal/nodes" 2>/dev/null || true)"
    if python3 -c 'import json,sys; expected=sys.argv[1]; d=json.load(sys.stdin); nodes=d.get("nodes",{}).values(); raise SystemExit(0 if any((n.get("ingest") or {}).get("status") == expected for n in nodes) else 1)' "$expected" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Node ingest status が $expected になりませんでした" >&2
  return 1
}

wait_node_event() {
  local expected="$1"
  local timeout="${2:-20}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    payload="$(node_admin_curl -fsS --max-time 3 "$base_url/internal/nodes" 2>/dev/null || true)"
    if python3 -c 'import json,sys; expected=sys.argv[1]; d=json.load(sys.stdin); nodes=d.get("nodes",{}).values(); raise SystemExit(0 if any(any(e.get("type") == expected for e in n.get("events",[])) for n in nodes) else 1)' "$expected" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Node event $expected が記録されませんでした" >&2
  return 1
}

assert_runtime_secret_boundaries() {
  # Continuity needs only its two node-local media URIs. It must not be able to
  # read the relay or external-destination volumes even when running as root.
  "${compose[@]}" exec -T continuity sh -c \
    'test ! -e /run/irlight/relay-secrets && test ! -e /run/irlight/egress-secrets'

  # Scan the live container PID namespace without printing any credential.
  # This catches regressions that put a generated URI or its password/query
  # token back into ffprobe (or another child process) argv.
  "${compose[@]}" exec -T node-agent python3 -c '
from pathlib import Path
import time
import urllib.parse
import sys

protected = []
for raw_path in sys.argv[1:]:
    path = Path(raw_path)
    try:
        uri = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"required secret file unavailable: {path.name}") from exc
    if not uri:
        raise SystemExit(f"required secret file empty: {path.name}")
    protected.append(uri.encode())
    parsed = urllib.parse.urlsplit(uri)
    candidates = [parsed.password or ""]
    for values in urllib.parse.parse_qs(parsed.query).values():
        candidates.extend(values)
    protected.extend(
        urllib.parse.unquote(value).encode()
        for value in candidates
        if len(value) >= 8
    )

deadline = time.monotonic() + 8.0
while time.monotonic() < deadline:
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            command = (process / "cmdline").read_bytes()
        except OSError:
            continue
        if any(value and value in command for value in protected):
            raise SystemExit("protected media credential present in process arguments")
    time.sleep(0.05)
' \
    /run/irlight/continuity-secrets/media_input_uri \
    /run/irlight/continuity-secrets/media_publish_uri \
    /run/irlight/relay-secrets/media_relay_uri \
    /run/irlight/egress-secrets/egress_url

  "${compose[@]}" logs --no-color 2>&1 | \
    "${compose[@]}" exec -T node-agent python3 -c '
from pathlib import Path
import sys

logs = sys.stdin.buffer.read()
for raw_path in sys.argv[1:]:
    try:
        value = Path(raw_path).read_text(encoding="utf-8").strip().encode()
    except OSError as exc:
        raise SystemExit("required secret file unavailable during log scan") from exc
    if value and value in logs:
        raise SystemExit("protected media credential present in container logs")
' \
      /run/irlight/continuity-secrets/media_input_uri \
      /run/irlight/continuity-secrets/media_publish_uri \
      /run/irlight/relay-secrets/media_relay_uri \
      /run/irlight/egress-secrets/egress_url
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

setup_control_plane_and_ingest() {
  local email password login csrf destination destination_id credential srt_url srt_payload auth_status
  email="smoke-$(date +%s)-$RANDOM@example.invalid"
  password="SmokePassword123!"
  rm -f "$auth_cookie_jar"

  curl -fsS --max-time 10 -X POST "$base_url/v1/auth/register" \
    -H 'Content-Type: application/json' \
    --data "{\"email\":\"$email\",\"password\":\"$password\",\"display_name\":\"Smoke\"}" >/dev/null

  login="$(curl -fsS --max-time 10 -c "$auth_cookie_jar" -X POST "$base_url/v1/auth/login" \
    -H 'Content-Type: application/json' \
    --data "{\"email\":\"$email\",\"password\":\"$password\"}")"
  csrf="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])' <<<"$login")"

  destination="$(curl -fsS --max-time 10 -b "$auth_cookie_jar" -X POST "$base_url/v1/destinations" \
    -H 'Content-Type: application/json' \
    -H "X-CSRF-Token: $csrf" \
    --data '{"type":"rtmp","display_name":"Local RTMP probe","server_url":"rtmp://mediamtx:1935/live/input","secret_ref":"smoke/rtmp"}')"
  destination_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$destination")"
  curl -fsS --max-time 10 -b "$auth_cookie_jar" -X POST \
    "$base_url/v1/destinations/$destination_id/verify" \
    -H "X-CSRF-Token: $csrf" >/dev/null

  ingest_session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
  curl -fsS --max-time 10 -b "$auth_cookie_jar" -X POST \
    "$base_url/v1/sessions/$ingest_session_id/prepare" \
    -H 'Content-Type: application/json' \
    -H "X-CSRF-Token: $csrf" \
    -H "Idempotency-Key: smoke-prepare-$ingest_session_id" \
    --data '{"environment":"dev"}' >/dev/null

  credential="$(curl -fsS --max-time 10 -b "$auth_cookie_jar" -X POST \
    "$base_url/v1/sessions/$ingest_session_id/ingest-credentials" \
    -H 'Content-Type: application/json' \
    -H "X-CSRF-Token: $csrf" \
    --data '{"protocols":["rtmp","srt"],"ttl_seconds":3600}')"
  ingest_username="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])' <<<"$credential")"
  ingest_secret="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["credential_secret"])' <<<"$credential")"

  auth_status="$(curl -sS -o /tmp/irlight-auth-reject.json -w '%{http_code}' --max-time 5 \
    -X POST "$base_url/internal/ingest/auth" \
    -H 'Content-Type: application/json' \
    --data "{\"user\":\"$ingest_username\",\"password\":\"wrong\",\"action\":\"publish\",\"path\":\"live/input\",\"protocol\":\"rtmp\"}")"
  if [[ "$auth_status" != "401" ]]; then
    echo "invalid ingest credential was not rejected (HTTP $auth_status)" >&2
    return 1
  fi

  srt_url="srt://mediamtx:8890?streamid=publish:live/input:${ingest_username}:${ingest_secret}"
  srt_payload="$(python3 -c 'import json,sys; print(json.dumps({"type":"srt","display_name":"Local SRT probe","server_url":sys.argv[1],"secret_ref":"smoke/srt"}))' "$srt_url")"
  destination="$(curl -fsS --max-time 10 -b "$auth_cookie_jar" -X POST "$base_url/v1/destinations" \
    -H 'Content-Type: application/json' \
    -H "X-CSRF-Token: $csrf" \
    --data "$srt_payload")"
  destination_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$destination")"
  curl -fsS --max-time 10 -b "$auth_cookie_jar" -X POST \
    "$base_url/v1/destinations/$destination_id/verify" \
    -H "X-CSRF-Token: $csrf" >/dev/null
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
wait_node_registered 45

current_stage="authenticated-ingest-setup"
setup_control_plane_and_ingest

current_stage="reject-unsupported-resolution"
start_publisher 15 640 360
wait_node_event "ingest.rejected" 15
if [[ -n "$publisher_pid" ]]; then
  kill "$publisher_pid" 2>/dev/null || true
  wait "$publisher_pid" 2>/dev/null || true
  publisher_pid=""
fi
wait_status \
  --timeout 15 \
  --session-status HOLDING \
  --video-source STANDBY

current_stage="first-live"
start_publisher 35 1280 720
wait_status \
  --timeout 45 \
  --session-status LIVE \
  --video-source LIVE \
  --audio-desired LIVE \
  --audio-actual LIVE
wait_node_ingest_status "ACCEPTED" 12

current_stage="runtime-secret-boundaries"
assert_runtime_secret_boundaries

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
