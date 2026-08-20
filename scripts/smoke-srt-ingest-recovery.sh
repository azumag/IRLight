#!/usr/bin/env bash
set -euo pipefail

tmp_dir="$(mktemp -d)"
override="$tmp_dir/srt-ingest.override.yml"
cookie_jar="$tmp_dir/cookies.txt"
publisher_log="$tmp_dir/srt-publisher.log"
base_url="${BASE_URL:-http://127.0.0.1:8080}"
disconnect_seconds="${SRT_DISCONNECT_SECONDS:-10}"
publisher_pid=""
email="srt-recovery-$(date +%s)-$RANDOM@example.invalid"
password='SmokePassword123!'

case "$disconnect_seconds" in
  5|10|30|120|600) ;;
  *)
    echo "SRT_DISCONNECT_SECONDS must be one of: 5, 10, 30, 120, 600" >&2
    exit 2
    ;;
esac

stage() {
  printf '\n=== srt-ingest-recovery stage: %s ===\n' "$1"
}

cat >"$override" <<'YAML'
services:
  control-ui:
    environment:
      NODE_BOOTSTRAP_TOKENS: srt-recovery-node-token
      NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT: "1"
      RECOVERY_STABLE_SECONDS: "3"
      SESSION_HOLD_TIMEOUT_SECONDS: "900"
  node-agent:
    environment:
      NODE_BOOTSTRAP_TOKEN: srt-recovery-node-token
      NODE_PROVIDER_SERVER_ID: ${ASSIGNED_PROVIDER_SERVER_ID:-unassigned-provider}
      NODE_BOOT_ID: srt-recovery-boot
      NODE_HEARTBEAT_INTERVAL: "2"
      NODE_INGEST_SAMPLE_SECONDS: "2"
      NODE_INGEST_SAMPLE_TIMEOUT_MARGIN_SECONDS: "2"
YAML

compose=(docker compose -f docker-compose.poc.yml -f "$override")

cleanup() {
  status=$?
  if [[ -n "$publisher_pid" ]]; then
    kill -INT "$publisher_pid" 2>/dev/null || true
    wait "$publisher_pid" 2>/dev/null || true
  fi
  if [[ $status -ne 0 ]]; then
    echo "--- publisher log ---" >&2
    tail -n 120 "$publisher_log" >&2 2>/dev/null || true
    echo "--- session ---" >&2
    session_json >&2 2>/dev/null || true
    echo "--- events ---" >&2
    session_events >&2 2>/dev/null || true
    echo "--- continuity state ---" >&2
    "${compose[@]}" exec -T continuity sh -c 'cat /state/status.json 2>/dev/null || true' >&2 || true
    echo "--- compose logs ---" >&2
    "${compose[@]}" logs --no-color --tail=180 >&2 || true
  fi
  rm -f "$cookie_jar"
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT

wait_http() {
  local url="$1" timeout="${2:-60}" deadline
  deadline=$((SECONDS + timeout))
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
events=payload if isinstance(payload,list) else payload.get("events",[])
print(max((int(e.get("sequence",0)) for e in events),default=0))
'
}

wait_session_status() {
  local expected="$1" timeout="${2:-75}" deadline payload
  deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    payload="$(session_json 2>/dev/null || true)"
    if python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("status")==sys.argv[1] else 1)' "$expected" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Session did not become $expected" >&2
  return 1
}

wait_holding_disconnect_after() {
  local after="$1" timeout="${2:-60}" deadline status_payload events_payload
  deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    status_payload="$(session_json 2>/dev/null || true)"
    events_payload="$(session_events 2>/dev/null || true)"
    if python3 -c '
import json,sys
session=json.loads(sys.argv[1]); after=int(sys.argv[2])
payload=json.load(sys.stdin); events=payload if isinstance(payload,list) else payload.get("events",[])
ok=session.get("status")=="HOLDING" and any(
 int(e.get("sequence",0))>after and e.get("type")=="session.holding" and e.get("reason_code")=="INGEST_DISCONNECTED"
 for e in events
)
raise SystemExit(0 if ok else 1)
' "$status_payload" "$after" <<<"$events_payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Session did not enter INGEST_DISCONNECTED HOLDING" >&2
  return 1
}

wait_assigned_node() {
  local timeout="${1:-45}" deadline payload
  deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    payload="$(curl -fsS --max-time 5 "$base_url/internal/nodes" 2>/dev/null || true)"
    if python3 -c '
import json,sys
sid=sys.argv[1]; d=json.load(sys.stdin)
raise SystemExit(0 if any(n.get("session_assigned") is True and n.get("session_id")==sid for n in d.get("nodes",{}).values()) else 1)
' "$session_id" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Node did not bind to Session" >&2
  return 1
}

wait_continuity_state() {
  local expected_status="$1" expected_video="$2" expected_audio="$3" timeout="${4:-75}" deadline payload
  deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    payload="$("${compose[@]}" exec -T continuity sh -c 'cat /state/status.json 2>/dev/null || true' || true)"
    if python3 -c '
import json,sys
d=json.load(sys.stdin)
actual=[d.get("session_status"),d.get("video_source"),d.get("actual_audio_mode")]
raise SystemExit(0 if actual==sys.argv[1:4] else 1)
' "$expected_status" "$expected_video" "$expected_audio" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  echo "Continuity did not become $expected_status/$expected_video/$expected_audio" >&2
  return 1
}

assert_output_relay_online() {
  "${compose[@]}" exec -T continuity python3 - <<'PY'
import json, urllib.request
with urllib.request.urlopen("http://mediamtx:9997/v3/paths/list?itemsPerPage=100",timeout=3) as response:
    payload=json.load(response)
relay=next((item for item in payload.get("items",[]) if item.get("name")=="output/relay"),None)
if relay is None or not relay.get("online"):
    raise SystemExit(f"output/relay not online: {relay!r}")
codecs={track.get("codec") for track in relay.get("tracks2",[]) if isinstance(track,dict)}
if not {"H264","MPEG-4 Audio"}.issubset(codecs):
    raise SystemExit(f"output/relay codecs unexpected: {sorted(codecs)!r}")
PY
}

start_publisher() {
  : >"$publisher_log"
  ffmpeg -hide_banner -loglevel warning -nostdin \
    -re -f lavfi -i 'testsrc2=size=1280x720:rate=30' \
    -re -f lavfi -i 'sine=frequency=440:sample_rate=48000' \
    -map 0:v:0 -map 1:a:0 \
    -c:v libx264 -preset veryfast -tune zerolatency -b:v 1200k -maxrate 1200k -bufsize 2400k \
    -g 60 -keyint_min 60 -bf 0 -pix_fmt yuv420p \
    -c:a aac -b:a 128k -ar 48000 -ac 2 \
    -f mpegts "$srt_url" >"$publisher_log" 2>&1 &
  publisher_pid=$!
}

stop_publisher() {
  if [[ -n "$publisher_pid" ]]; then
    kill -INT "$publisher_pid" 2>/dev/null || true
    wait "$publisher_pid" 2>/dev/null || true
    publisher_pid=""
  fi
}

assert_recovery_events() {
  local after="$1"
  session_events | python3 -c '
import json,sys
after=int(sys.argv[1]); payload=json.load(sys.stdin)
events=[e for e in (payload if isinstance(payload,list) else payload.get("events",[])) if int(e.get("sequence",0))>after]
hold=next((e for e in events if e.get("type")=="session.holding" and e.get("reason_code")=="INGEST_DISCONNECTED"),None)
if hold is None: raise SystemExit(f"missing disconnect HOLDING: {events!r}")
reconnected=next((e for e in events if int(e.get("sequence",0))>int(hold.get("sequence",0)) and e.get("type")=="ingest.reconnected"),None)
recovered=next((e for e in events if int(e.get("sequence",0))>int(hold.get("sequence",0)) and e.get("type")=="session.recovered"),None)
if reconnected is None: raise SystemExit(f"missing ingest.reconnected: {events!r}")
if recovered is None: raise SystemExit(f"missing session.recovered: {events!r}")
if any(e.get("type") in {"session.failed","session.cleanup_failed"} for e in events):
    raise SystemExit(f"fatal event during SRT recovery: {events!r}")
print("SRT event sequence: session.holding -> ingest.reconnected -> session.recovered")
' "$after"
}

command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg is required" >&2; exit 2; }

stage "start control plane"
"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" up -d --build control-ui
wait_http "$base_url/healthz" 60

curl -fsS --max-time 10 -X POST "$base_url/v1/auth/register" \
  -H 'Content-Type: application/json' \
  --data "{\"email\":\"$email\",\"password\":\"$password\",\"display_name\":\"SRT Recovery Smoke\"}" >/dev/null
login

session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
prepared="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/prepare" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  -H "Idempotency-Key: srt-recovery-$session_id" \
  --data '{"environment":"dev"}')"
provider_server_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["provider_server_id"])' <<<"$prepared")"
[[ -n "$provider_server_id" && "$provider_server_id" != "None" ]] || { echo "prepare did not allocate provider" >&2; exit 1; }

export ASSIGNED_PROVIDER_SERVER_ID="$provider_server_id"
"${compose[@]}" up -d --build --no-deps node-agent
wait_assigned_node 45
stage "node assigned"

credential="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/ingest-credentials" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  --data '{"protocols":["srt"],"ttl_seconds":1800}')"
ingest_username="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])' <<<"$credential")"
ingest_secret="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["credential_secret"])' <<<"$credential")"
streamid="publish:live/input:${ingest_username}:${ingest_secret}"
srt_url="srt://127.0.0.1:8890?mode=caller&streamid=${streamid}"

baseline_sequence="$(latest_event_sequence)"
start_publisher
stage "SRT publisher started"
wait_session_status LIVE 75
wait_continuity_state LIVE LIVE LIVE 75
assert_output_relay_online
kill -0 "$publisher_pid" 2>/dev/null || { echo "SRT publisher exited before LIVE" >&2; exit 1; }
stage "initial SRT LIVE"

stop_publisher
wait_holding_disconnect_after "$baseline_sequence" 60
wait_continuity_state HOLDING STANDBY SILENT_FALLBACK 60
assert_output_relay_online
stage "SRT disconnect HOLDING with relay online"

sleep "$disconnect_seconds"
wait_session_status HOLDING 5
assert_output_relay_online

start_publisher
stage "SRT publisher restarted"
wait_session_status LIVE 90
wait_continuity_state LIVE LIVE LIVE 90
assert_output_relay_online
kill -0 "$publisher_pid" 2>/dev/null || { echo "SRT publisher exited before recovered LIVE" >&2; exit 1; }
assert_recovery_events "$baseline_sequence"

stage "SRT recovery verified"
echo "SRT ingest recovery smoke passed (${disconnect_seconds}s disconnect)"
