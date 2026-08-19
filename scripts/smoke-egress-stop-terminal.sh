#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp_dir="$(mktemp -d)"
override="$tmp_dir/egress-stop-terminal.override.yml"
secret_file="$tmp_dir/egress_url"
stream_key="ci-egress-stop-secret-$RANDOM"
export EGRESS_SECRET_FILE="$secret_file"

cat >"$secret_file" <<EOF
rtmp://egress-target:1935/live/$stream_key
EOF
chmod 600 "$secret_file"

cat >"$override" <<'YAML'
services:
  egress-target:
    image: bluenviron/mediamtx:1.20.0
    restart: "no"

  egress-gateway:
    build:
      context: ./apps/egress-gateway
      dockerfile: Dockerfile
    restart: "no"
    depends_on:
      - mediamtx
      - continuity
      - egress-target
    environment:
      EGRESS_INPUT_URI: rtsp://mediamtx:8554/output/relay
      EGRESS_URL_FILE: /run/irlight/secrets/egress_url
      EGRESS_STATUS_FILE: /state/egress.json
      # The first phase uses an isolated Compose target on RFC1918 space.
      EGRESS_ALLOW_PRIVATE_TARGETS: "1"
      EGRESS_CONNECT_TIMEOUT_SECONDS: "10"
      # Keep the reconnect window deliberately long so SIGTERM races with the
      # backoff wait rather than the next connection attempt.
      EGRESS_RETRY_INITIAL_SECONDS: "30"
      EGRESS_RETRY_MAX_SECONDS: "30"
      EGRESS_RETRY_MULTIPLIER: "2"
      EGRESS_RETRY_JITTER_RATIO: "0"
      EGRESS_MAX_ATTEMPTS: "0"
      EGRESS_MAX_RETRY_SECONDS: "0"
    volumes:
      - irlight-state:/state
      - ${EGRESS_SECRET_FILE}:/run/irlight/secrets/egress_url:ro
YAML

compose=(docker compose -f "$repo_root/docker-compose.poc.yml" -f "$override")

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps -a >&2 || true
    echo "--- continuity logs ---" >&2
    "${compose[@]}" logs --no-color --tail=120 continuity >&2 || true
    echo "--- egress gateway logs ---" >&2
    "${compose[@]}" logs --no-color --tail=160 egress-gateway >&2 || true
    echo "--- target logs ---" >&2
    "${compose[@]}" logs --no-color --tail=120 egress-target >&2 || true
  fi
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT

read_egress_status() {
  # continuity shares irlight-state and remains alive even after the Gateway is
  # explicitly stopped, so it is a stable observer of the final status file.
  "${compose[@]}" exec -T continuity cat /state/egress.json 2>/dev/null || true
}

wait_egress_status() {
  local expected="$1"
  local timeout="${2:-45}"
  local deadline=$((SECONDS + timeout))
  local payload=""
  while (( SECONDS < deadline )); do
    payload="$(read_egress_status)"
    if python3 -c '
import json,sys
expected=sys.argv[1]
try:
    value=json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if value.get("status") == expected else 1)
' "$expected" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "egress status did not become $expected; last=$payload" >&2
  return 1
}

assert_status_reason() {
  local expected_status="$1"
  local expected_reason="$2"
  local payload
  payload="$(read_egress_status)"
  python3 -c '
import json,sys
value=json.loads(sys.argv[1])
expected_status=sys.argv[2]
expected_reason=sys.argv[3]
assert value.get("status") == expected_status, value
assert value.get("reason_code") == expected_reason, value
' "$payload" "$expected_status" "$expected_reason"
}

"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" up -d --build mediamtx continuity egress-target egress-gateway
wait_egress_status CONNECTED 60

# Phase 1: remote outage enters a long reconnect backoff. An explicit user stop
# must interrupt that wait, write STOPPED, and never reconnect after the target
# comes back.
"${compose[@]}" stop egress-target >/dev/null
wait_egress_status RECONNECTING 45

before_stop="$(read_egress_status)"
python3 -c '
import json,sys,time
value=json.loads(sys.argv[1])
assert value.get("status") == "RECONNECTING", value
next_retry=value.get("next_retry_at")
assert isinstance(next_retry, (int, float)), value
assert next_retry - time.time() > 10, value
' "$before_stop"

"${compose[@]}" stop -t 5 egress-gateway >/dev/null
wait_egress_status STOPPED 10
assert_status_reason STOPPED USER_STOPPED

if "${compose[@]}" ps --status running --services | grep -qx egress-gateway; then
  echo "egress gateway is still running after explicit stop" >&2
  exit 1
fi
if ! "${compose[@]}" ps --status running --services | grep -qx continuity; then
  echo "continuity stopped during egress stop/reconnect race" >&2
  exit 1
fi

"${compose[@]}" start egress-target >/dev/null
sleep 5
if "${compose[@]}" ps --status running --services | grep -qx egress-gateway; then
  echo "egress gateway restarted after target recovery despite user stop" >&2
  exit 1
fi
assert_status_reason STOPPED USER_STOPPED

# Phase 2: an unsafe metadata/private destination must fail before GStreamer
# attempts to connect and must not enter the reconnect loop.
unsafe_secret="unsafe-stop-secret-$RANDOM"
cat >"$secret_file" <<EOF
rtmp://169.254.169.254/live/$unsafe_secret
EOF
chmod 600 "$secret_file"

set +e
terminal_output="$("${compose[@]}" run --rm --no-deps \
  -e EGRESS_ALLOW_PRIVATE_TARGETS=0 \
  egress-gateway 2>&1)"
terminal_rc=$?
set -e

if [[ $terminal_rc -ne 2 ]]; then
  echo "unsafe destination did not exit with terminal status: rc=$terminal_rc" >&2
  echo "$terminal_output" >&2
  exit 1
fi
wait_egress_status FAILED 5
assert_status_reason FAILED DESTINATION_UNSAFE

if grep -Fq "$unsafe_secret" <<<"$terminal_output"; then
  echo "terminal guard output leaked destination secret" >&2
  exit 1
fi
if grep -Fq "$stream_key" <<<"$("${compose[@]}" logs --no-color egress-gateway 2>/dev/null || true)"; then
  echo "egress gateway logs leaked stream key" >&2
  exit 1
fi

echo "IRLight egress stop-race and terminal-failure smoke passed."
