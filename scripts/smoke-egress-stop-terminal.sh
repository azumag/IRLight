#!/usr/bin/env bash
set -euo pipefail
umask 077
export EGRESS_RTMP_SINK_FACTORY="${EGRESS_RTMP_SINK_FACTORY:-rtmpsink}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
smoke_project="irlight-egress-stop-terminal-smoke-$$-$RANDOM"
tmp_dir="$(mktemp -d)"
override="$tmp_dir/egress-stop-terminal.override.yml"
secret_file="$tmp_dir/egress_url"
redaction_values_file="$tmp_dir/redaction-values"
stream_key="ci-egress-stop-secret-$RANDOM"
export EGRESS_SECRET_FILE="$secret_file"

printf '%s\n' "$stream_key" >"$redaction_values_file"
cat >"$secret_file" <<EOF
rtmp://egress-target:1935/live/$stream_key
EOF
chmod 600 "$secret_file" "$redaction_values_file"
unset stream_key

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
      EGRESS_INPUT_URI_FILE: /run/irlight/relay-secrets/media_relay_uri
      EGRESS_RTMP_SINK_FACTORY: "${EGRESS_RTMP_SINK_FACTORY}"
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
      - irlight-relay-secrets:/run/irlight/relay-secrets:ro
      - ${EGRESS_SECRET_FILE}:/run/irlight/secrets/egress_url:ro
YAML

compose=(docker compose -p "$smoke_project" -f "$repo_root/docker-compose.poc.yml" -f "$override")

emit_failure_stage() {
  local stage="$1"
  printf '::error title=IRLight docker smoke failure::stage=%s\n' "$stage" >&2
}

redact_generated_secrets() {
  python3 -c '
from pathlib import Path
import sys

secrets = [value for value in Path(sys.argv[1]).read_bytes().splitlines() if value]
if not secrets:
    raise SystemExit(1)
data = sys.stdin.buffer.read()
for secret in secrets:
    data = data.replace(secret, b"<redacted>")
sys.stdout.buffer.write(data)
' "$redaction_values_file"
}

stdin_excludes_generated_secrets() {
  python3 -c '
from pathlib import Path
import sys

secrets = [value for value in Path(sys.argv[1]).read_bytes().splitlines() if value]
data = sys.stdin.buffer.read()
raise SystemExit(0 if secrets and all(secret not in data for secret in secrets) else 1)
' "$redaction_values_file"
}

file_excludes_generated_secrets() {
  local candidate="$1"
  python3 -c '
from pathlib import Path
import sys

secrets = [value for value in Path(sys.argv[1]).read_bytes().splitlines() if value]
data = Path(sys.argv[2]).read_bytes()
raise SystemExit(0 if secrets and all(secret not in data for secret in secrets) else 1)
' "$redaction_values_file" "$candidate"
}

status_matches_reason() {
  local expected_status="$1"
  local expected_reason="$2"
  python3 -c '
import json,sys
try:
    value=json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
raise SystemExit(
    0
    if value.get("status") == sys.argv[1]
    and value.get("reason_code") == sys.argv[2]
    else 1
)
' "$expected_status" "$expected_reason"
}

status_has_long_reconnect_backoff() {
  python3 -c '
import json,sys,time
try:
    value=json.load(sys.stdin)
    next_retry=value.get("next_retry_at")
    valid=(
        value.get("status") == "RECONNECTING"
        and isinstance(next_retry, (int, float))
        and not isinstance(next_retry, bool)
        and next_retry - time.time() > 10
    )
except Exception:
    valid=False
raise SystemExit(0 if valid else 1)
'
}

emit_redacted_compose_logs() {
  local service="$1"
  local tail_lines="$2"
  local raw_file="$tmp_dir/${service}.failure.log"
  local logs_rc=0

  "${compose[@]}" logs --no-color --tail="$tail_lines" "$service" >"$raw_file" 2>&1 || logs_rc=$?
  if ! redact_generated_secrets <"$raw_file" >&2; then
    echo "failed to redact $service diagnostics; output withheld" >&2
    return 1
  fi
  if (( logs_rc != 0 )); then
    echo "failed to read $service diagnostics: rc=$logs_rc" >&2
    return "$logs_rc"
  fi
}

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps -a >&2 || true
    echo "--- continuity logs (redacted) ---" >&2
    emit_redacted_compose_logs continuity 120 || true
    echo "--- egress gateway logs (redacted) ---" >&2
    emit_redacted_compose_logs egress-gateway 160 || true
    echo "--- target logs (redacted) ---" >&2
    emit_redacted_compose_logs egress-target 120 || true
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
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
  local safe_payload
  if safe_payload="$(printf '%s' "$payload" | redact_generated_secrets 2>/dev/null)"; then
    echo "egress status did not become $expected; last=$safe_payload" >&2
  else
    echo "egress status did not become $expected; last=<redaction-failed>" >&2
  fi
  return 1
}

assert_status_reason() {
  local expected_status="$1"
  local expected_reason="$2"
  local payload
  payload="$(read_egress_status)"
  status_matches_reason "$expected_status" "$expected_reason" <<<"$payload"
}

if ! "${compose[@]}" config >/dev/null; then
  emit_failure_stage "compose-config"
  exit 1
fi
# Start Node Agent and Control Plane as well: Continuity consumes authenticated
# local-media URIs from the Agent-owned tmpfs secret volume.
if ! "${compose[@]}" up -d --build; then
  emit_failure_stage "compose-up"
  exit 1
fi
if ! wait_egress_status CONNECTED 60; then
  emit_failure_stage "initial-connected"
  exit 1
fi

# Phase 1: remote outage enters a long reconnect backoff. An explicit user stop
# must interrupt that wait, write STOPPED, and never reconnect after the target
# comes back.
if ! "${compose[@]}" stop egress-target >/dev/null; then
  emit_failure_stage "target-stop"
  exit 1
fi
if ! wait_egress_status RECONNECTING 45; then
  emit_failure_stage "reconnecting"
  exit 1
fi

before_stop="$(read_egress_status)"
if ! status_has_long_reconnect_backoff <<<"$before_stop"; then
  emit_failure_stage "backoff-window"
  exit 1
fi

if ! "${compose[@]}" stop -t 5 egress-gateway >/dev/null; then
  emit_failure_stage "gateway-stop"
  exit 1
fi
if ! wait_egress_status STOPPED 10; then
  emit_failure_stage "stopped-user-stopped"
  exit 1
fi
if ! assert_status_reason STOPPED USER_STOPPED; then
  emit_failure_stage "stopped-user-stopped"
  exit 1
fi

if "${compose[@]}" ps --status running --services | grep -qx egress-gateway; then
  echo "egress gateway is still running after explicit stop" >&2
  emit_failure_stage "gateway-still-running"
  exit 1
fi
if ! "${compose[@]}" ps --status running --services | grep -qx continuity; then
  echo "continuity stopped during egress stop/reconnect race" >&2
  emit_failure_stage "continuity-survives-stop"
  exit 1
fi

if ! "${compose[@]}" start egress-target >/dev/null; then
  emit_failure_stage "target-recovery-start"
  exit 1
fi
sleep 5
if "${compose[@]}" ps --status running --services | grep -qx egress-gateway; then
  echo "egress gateway restarted after target recovery despite user stop" >&2
  emit_failure_stage "target-recovery-no-restart"
  exit 1
fi
if ! assert_status_reason STOPPED USER_STOPPED; then
  emit_failure_stage "target-recovery-stopped-status"
  exit 1
fi

# Phase 2: an unsafe metadata/private destination must fail before GStreamer
# attempts to connect and must not enter the reconnect loop.
unsafe_secret="unsafe-stop-secret-$RANDOM"
printf '%s\n' "$unsafe_secret" >>"$redaction_values_file"
cat >"$secret_file" <<EOF
rtmp://169.254.169.254/live/$unsafe_secret
EOF
chmod 600 "$secret_file" "$redaction_values_file"
unset unsafe_secret

set +e
terminal_output="$("${compose[@]}" run --rm --no-deps \
  -e EGRESS_ALLOW_PRIVATE_TARGETS=0 \
  egress-gateway 2>&1)"
terminal_rc=$?
set -e

if [[ $terminal_rc -ne 2 ]]; then
  echo "unsafe destination did not exit with terminal status: rc=$terminal_rc" >&2
  if ! printf '%s\n' "$terminal_output" | redact_generated_secrets >&2; then
    echo "failed to redact terminal diagnostics; output withheld" >&2
  fi
  emit_failure_stage "unsafe-destination-terminal"
  exit 1
fi
if ! wait_egress_status FAILED 5; then
  emit_failure_stage "unsafe-destination-failed"
  exit 1
fi
if ! assert_status_reason FAILED DESTINATION_UNSAFE; then
  emit_failure_stage "unsafe-destination-reason"
  exit 1
fi

if ! stdin_excludes_generated_secrets <<<"$terminal_output"; then
  echo "terminal guard output generated-secret check failed closed" >&2
  emit_failure_stage "secret-redaction-terminal-output"
  exit 1
fi

egress_logs_file="$tmp_dir/egress-gateway.log"
if ! "${compose[@]}" logs --no-color egress-gateway >"$egress_logs_file" 2>&1; then
  echo "failed to read egress gateway logs for secret redaction check" >&2
  emit_failure_stage "secret-redaction-logs-read"
  exit 1
fi
if ! file_excludes_generated_secrets "$egress_logs_file"; then
  echo "egress gateway log generated-secret check failed closed" >&2
  emit_failure_stage "secret-redaction-logs"
  exit 1
fi

echo "IRLight egress stop-race and terminal-failure smoke passed."
