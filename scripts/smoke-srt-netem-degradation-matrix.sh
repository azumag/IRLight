#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/node-admin.sh"

tmp_dir="$(mktemp -d)"
override="$tmp_dir/srt-netem-matrix.override.yml"
cookie_jar="$tmp_dir/cookies.txt"
base_url="${BASE_URL:-http://127.0.0.1:8080}"
profile_seconds="${NETEM_PROFILE_SECONDS:-12}"
selected_profiles="${NETEM_MATRIX_PROFILES:-loss-1,loss-3,loss-5,loss-10,latency-jitter,bandwidth-800k}"
publisher_name="irlight-srt-netem-matrix-publisher-$RANDOM-$RANDOM"
publisher_log="$tmp_dir/publisher.log"
publisher_pid=""
netem_applied=0
email="srt-netem-matrix-$(date +%s)-$RANDOM@example.invalid"
password='SmokePassword123!'

case "$profile_seconds" in
  ''|*[!0-9]*)
    echo "NETEM_PROFILE_SECONDS must be a positive integer" >&2
    exit 2
    ;;
esac
if (( profile_seconds < 5 || profile_seconds > 60 )); then
  echo "NETEM_PROFILE_SECONDS must be between 5 and 60" >&2
  exit 2
fi

stage() {
  printf '\n=== srt-netem-matrix stage: %s ===\n' "$1"
}

cat >"$override" <<'YAML'
services:
  control-ui:
    environment:
      NODE_BOOTSTRAP_TOKENS: srt-netem-matrix-node-token
      NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT: "1"
      RECOVERY_STABLE_SECONDS: "3"
      SESSION_HOLD_TIMEOUT_SECONDS: "300"
  node-agent:
    environment:
      NODE_BOOTSTRAP_TOKEN: srt-netem-matrix-node-token
      NODE_PROVIDER_SERVER_ID: ${ASSIGNED_PROVIDER_SERVER_ID:-unassigned-provider}
      NODE_BOOT_ID: srt-netem-matrix-boot
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
    cat "$publisher_log" >&2 2>/dev/null || true
    echo "--- session ---" >&2
    session_json >&2 2>/dev/null || true
    echo "--- events ---" >&2
    session_events >&2 2>/dev/null || true
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
  local timeout="${2:-90}"
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

assert_session_nonterminal() {
  local payload
  payload="$(session_json)"
  python3 -c '
import json,sys
s=json.load(sys.stdin)
status=s.get("status")
if status in {"FAILED", "FAILED_CLEANUP", "FINISHED", "STOPPING"}:
    raise SystemExit(f"session entered terminal/teardown state during impairment: {status}")
print(status)
' <<<"$payload"
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

wait_continuity_live() {
  local timeout="${1:-90}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if continuity_state_matches LIVE LIVE LIVE; then
      return 0
    fi
    sleep 0.5
  done
  if continuity_state_matches LIVE LIVE LIVE; then
    return 0
  fi
  echo "Continuity did not return to LIVE/LIVE/LIVE" >&2
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
  docker exec "$publisher_name" gst-inspect-1.0 srtsink >/dev/null
}

start_publisher() {
  docker exec -i \
    -e IRLIGHT_PUBLISH_USER="$ingest_username" \
    -e IRLIGHT_PUBLISH_PASS="$ingest_secret" \
    "$publisher_name" python3 - <<'PY' >"$publisher_log" 2>&1 &
from __future__ import annotations

import os
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)
user = os.environ["IRLIGHT_PUBLISH_USER"]
password = os.environ["IRLIGHT_PUBLISH_PASS"]
url = f"srt://mediamtx:8890?mode=caller&streamid=publish:live/input:{user}:{password}"
description = f'''mpegtsmux name=mux alignment=7 ! srtsink uri="{url}"
videotestsrc is-live=true pattern=smpte !
  video/x-raw,width=1280,height=720,framerate=30/1,format=I420 !
  x264enc tune=zerolatency speed-preset=veryfast bitrate=1200 key-int-max=60 bframes=0 !
  video/x-h264,profile=main ! h264parse config-interval=-1 ! queue ! mux.
audiotestsrc is-live=true wave=sine freq=440 ! audioconvert ! audioresample !
  audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=128000 ! aacparse ! queue ! mux.'''

attempt = 0
while True:
    attempt += 1
    pipeline = Gst.parse_launch(description)
    bus = pipeline.get_bus()
    started = pipeline.set_state(Gst.State.PLAYING)
    if started == Gst.StateChangeReturn.FAILURE:
        print(f"publisher attempt {attempt} failed to start; retrying", flush=True)
        pipeline.set_state(Gst.State.NULL)
        time.sleep(1)
        continue
    print(f"publisher attempt {attempt} started", flush=True)
    try:
        while True:
            message = bus.timed_pop_filtered(
                100 * Gst.MSECOND,
                Gst.MessageType.ERROR | Gst.MessageType.EOS,
            )
            if message is None:
                time.sleep(0.05)
                continue
            if message.type == Gst.MessageType.ERROR:
                err, debug = message.parse_error()
                print(
                    f"publisher attempt {attempt} error: {err}; {debug}; retrying",
                    flush=True,
                )
            else:
                print(f"publisher attempt {attempt} reached EOS; retrying", flush=True)
            break
    finally:
        pipeline.set_state(Gst.State.NULL)
    time.sleep(1)
PY
  publisher_pid=$!
}

profile_args() {
  case "$1" in
    loss-1) printf '%s\n' '--loss' '1' ;;
    loss-3) printf '%s\n' '--loss' '3' ;;
    loss-5) printf '%s\n' '--loss' '5' ;;
    loss-10) printf '%s\n' '--loss' '10' ;;
    latency-jitter) printf '%s\n' '--delay-ms' '250' '--jitter-ms' '100' ;;
    bandwidth-800k) printf '%s\n' '--rate' '800kbit' ;;
    *) echo "unknown NETEM matrix profile: $1" >&2; return 2 ;;
  esac
}

profile_holding_summary() {
  local after_sequence="$1"
  session_events | python3 -c '
import json,sys
after=int(sys.argv[1])
payload=json.load(sys.stdin)
events=payload if isinstance(payload, list) else payload.get("events", [])
holds=[e for e in events if int(e.get("sequence", 0)) > after and e.get("type") == "session.holding"]
if not holds:
    print("none")
else:
    latest=holds[-1]
    print("{}|{}".format(latest.get("sequence"), latest.get("reason_code") or "UNKNOWN"))
' "$after_sequence"
}

assert_profile_events_safe() {
  local after_sequence="$1"
  session_events | python3 -c '
import json,sys
after=int(sys.argv[1])
payload=json.load(sys.stdin)
events=[
    e for e in (payload if isinstance(payload, list) else payload.get("events", []))
    if int(e.get("sequence", 0)) > after
]
fatal=[e for e in events if e.get("type") in {"session.failed", "session.cleanup_failed"}]
if fatal:
    raise SystemExit(f"fatal Session event during netem profile: {fatal!r}")
holds=[e for e in events if e.get("type") == "session.holding"]
if holds:
    last_hold=holds[-1]
    recovered=next((
        e for e in events
        if int(e.get("sequence", 0)) > int(last_hold.get("sequence", 0))
        and e.get("type") == "session.recovered"
    ), None)
    if recovered is None:
        raise SystemExit(f"Session held but did not recover after profile: {events!r}")
    print("holding={} recovered=yes".format(last_hold.get("reason_code") or "UNKNOWN"))
else:
    print("holding=none recovered=n/a")
' "$after_sequence"
}

run_profile() {
  local profile="$1"
  local baseline_sequence deadline status hold_summary event_summary
  local -a args=()
  mapfile -t args < <(profile_args "$profile")

  wait_session_status LIVE 90
  wait_continuity_live 90
  assert_output_relay_online
  baseline_sequence="$(latest_event_sequence)"

  bash ./scripts/netem-container.sh apply "$publisher_name" "${args[@]}"
  netem_applied=1
  stage "profile=$profile applied args=${args[*]}"
  bash ./scripts/netem-container.sh show "$publisher_name"

  deadline=$((SECONDS + profile_seconds))
  while (( SECONDS < deadline )); do
    assert_output_relay_online
    if ! kill -0 "$publisher_pid" 2>/dev/null; then
      echo "publisher supervisor exited during profile=$profile" >&2
      return 1
    fi
    status="$(assert_session_nonterminal)"
    printf 'profile=%s protocol=srt session_status=%s relay=online\n' "$profile" "$status"
    sleep 2
  done

  hold_summary="$(profile_holding_summary "$baseline_sequence")"
  bash ./scripts/netem-container.sh clear "$publisher_name"
  netem_applied=0
  stage "profile=$profile cleared observed_hold=$hold_summary"

  wait_session_status LIVE 90
  wait_continuity_live 90
  assert_output_relay_online
  if ! kill -0 "$publisher_pid" 2>/dev/null; then
    echo "publisher supervisor exited before LIVE recovery for profile=$profile" >&2
    return 1
  fi
  event_summary="$(assert_profile_events_safe "$baseline_sequence")"
  printf 'profile=%s protocol=srt result=PASS observed_hold=%s %s\n' "$profile" "$hold_summary" "$event_summary"
}

stage "start control plane"
"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" up -d --build control-ui
wait_http "$base_url/healthz" 60
stage "control plane ready"

curl -fsS --max-time 10 -X POST "$base_url/v1/auth/register" \
  -H 'Content-Type: application/json' \
  --data "{\"email\":\"$email\",\"password\":\"$password\",\"display_name\":\"SRT Netem Matrix Smoke\"}" >/dev/null
login

session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
prepared="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/prepare" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  -H "Idempotency-Key: srt-netem-matrix-$session_id" \
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
  --data '{"protocols":["srt"],"ttl_seconds":1800}')"
ingest_username="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])' <<<"$credential")"
ingest_secret="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["credential_secret"])' <<<"$credential")"

create_publisher_container
start_publisher
stage "SRT publisher supervisor started"
wait_session_status LIVE 75
wait_continuity_live 75
assert_output_relay_online
stage "initial SRT LIVE and relay online"

IFS=',' read -r -a profiles <<<"$selected_profiles"
if (( ${#profiles[@]} == 0 )); then
  echo "NETEM_MATRIX_PROFILES must contain at least one profile" >&2
  exit 2
fi

for profile in "${profiles[@]}"; do
  [[ -n "$profile" ]] || continue
  run_profile "$profile"
done

stage "all selected SRT netem degradation profiles passed"
echo "SRT netem degradation matrix passed profiles=$selected_profiles duration=${profile_seconds}s"
