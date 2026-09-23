#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
smoke_project="irlight-egress-isolated-auth-terminal-smoke-$$-$RANDOM"
tmp_dir="$(mktemp -d)"
override="$tmp_dir/egress-isolated-auth-terminal.override.yml"
secret_file="$tmp_dir/egress_url"
redaction_values_file="$tmp_dir/redaction-values"
auth_user="ci-auth-user-$RANDOM"
expected_pass="ci-auth-expected-$RANDOM"
wrong_pass="ci-auth-wrong-$RANDOM"
stream_name="ci-auth-stream-$RANDOM"
export EGRESS_SECRET_FILE="$secret_file"

printf '%s\n%s\n%s\n%s\n' "$auth_user" "$expected_pass" "$wrong_pass" "$stream_name" >"$redaction_values_file"
cat >"$secret_file" <<EOF
rtmp://egress-target:1935/live/$stream_name?user=$auth_user&pass=$wrong_pass
EOF
chmod 600 "$secret_file" "$redaction_values_file"

cat >"$override" <<YAML
services:
  egress-target:
    image: bluenviron/mediamtx:1.20.0
    restart: "no"
    environment:
      MTX_AUTHINTERNALUSERS_0_USER: "$auth_user"
      MTX_AUTHINTERNALUSERS_0_PASS: "$expected_pass"
      MTX_AUTHINTERNALUSERS_0_PERMISSIONS_0_ACTION: "publish"

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
      EGRESS_RTMP_SINK_FACTORY: "rtmpsink"
      EGRESS_LEGACY_PROCESS_ISOLATION_CANARY: "1"
      EGRESS_URL_FILE: /run/irlight/secrets/egress_url
      EGRESS_STATUS_FILE: /state/egress.json
      EGRESS_ALLOW_PRIVATE_TARGETS: "1"
      EGRESS_CONNECT_TIMEOUT_SECONDS: "10"
      EGRESS_RETRY_INITIAL_SECONDS: "1"
      EGRESS_RETRY_MAX_SECONDS: "2"
      EGRESS_RETRY_MULTIPLIER: "2"
      EGRESS_RETRY_JITTER_RATIO: "0"
      EGRESS_MAX_ATTEMPTS: "0"
      EGRESS_MAX_RETRY_SECONDS: "0"
    volumes:
      - irlight-state:/state
      - irlight-relay-secrets:/run/irlight/relay-secrets:ro
      - \${EGRESS_SECRET_FILE}:/run/irlight/secrets/egress_url:ro
YAML
unset auth_user expected_pass wrong_pass stream_name

compose=(docker compose -p "$smoke_project" -f "$repo_root/docker-compose.poc.yml" -f "$override")

emit_failure_stage() {
  printf '::error title=IRLight isolated egress auth-terminal smoke failure::stage=%s\n' "$1" >&2
}

redact_generated_secrets() {
  python3 -c '
from pathlib import Path
import sys
values = [v for v in Path(sys.argv[1]).read_bytes().splitlines() if v]
if not values:
    raise SystemExit(1)
data = sys.stdin.buffer.read()
for value in values:
    data = data.replace(value, b"<redacted>")
sys.stdout.buffer.write(data)
' "$redaction_values_file"
}

file_excludes_generated_secrets() {
  local candidate="$1"
  python3 -c '
from pathlib import Path
import sys
values = [v for v in Path(sys.argv[1]).read_bytes().splitlines() if v]
data = Path(sys.argv[2]).read_bytes()
raise SystemExit(0 if values and all(value not in data for value in values) else 1)
' "$redaction_values_file" "$candidate"
}

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps (secret-safe) ---" >&2
    "${compose[@]}" ps -a >&2 || true
    raw_logs="$tmp_dir/egress-gateway.failure.log"
    if "${compose[@]}" logs --no-color --tail=240 egress-gateway >"$raw_logs" 2>&1; then
      echo "--- egress gateway logs (redacted) ---" >&2
      redact_generated_secrets <"$raw_logs" >&2 || echo "redaction failed; log output withheld" >&2
    fi
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT

read_egress_status() {
  "${compose[@]}" exec -T continuity cat /state/egress.json 2>/dev/null || true
}

terminal_auth_status() {
  read_egress_status | python3 -c '
import json
import sys
try:
    value = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
reason = value.get("reason_code")
status = value.get("status")
attempt = value.get("attempt")
valid_attempt = isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 1
valid = (
    value.get("connected") is False
    and value.get("next_retry_at") is None
    and valid_attempt
    and (
        (reason == "AUTH_FAILED" and status == "AUTH_FAILED")
        or (reason == "PUBLISH_REJECTED" and status == "FAILED")
    )
)
raise SystemExit(0 if valid else 1)
' 2>/dev/null
}

read_terminal_attempt() {
  read_egress_status | python3 -c '
import json
import sys
try:
    value = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
attempt = value.get("attempt")
if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
    raise SystemExit(1)
print(attempt)
' 2>/dev/null
}

wait_terminal_auth_status() {
  local timeout="${1:-60}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if terminal_auth_status; then
      return 0
    fi
    sleep 1
  done
  terminal_auth_status
}

if ! "${compose[@]}" config >/dev/null; then
  emit_failure_stage "compose-config"
  exit 1
fi
if ! "${compose[@]}" up -d --build; then
  emit_failure_stage "compose-up"
  exit 1
fi

# MediaMTX requires credentials while the Gateway intentionally supplies a bad
# generated password. librtmp can preserve the server auth text as AUTH_FAILED,
# or collapse a pre-connect RTMP rejection to Gst.ResourceError.WRITE, which the
# existing policy maps to terminal PUBLISH_REJECTED. Both are terminal contracts;
# retrying either result would be a regression.
if ! wait_terminal_auth_status 60; then
  emit_failure_stage "terminal-auth-status"
  exit 1
fi
if ! terminal_attempt_before="$(read_terminal_attempt)"; then
  emit_failure_stage "terminal-auth-attempt"
  exit 1
fi

parent_container="$("${compose[@]}" ps -aq egress-gateway)"
if [[ -z "$parent_container" ]]; then
  emit_failure_stage "gateway-container-id"
  exit 1
fi

# A terminal child result must make the parent exit instead of entering retry.
for _ in $(seq 1 10); do
  if ! "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx egress-gateway; then
    break
  fi
  sleep 1
done
if "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx egress-gateway; then
  emit_failure_stage "gateway-retried-terminal-auth"
  exit 1
fi

exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$parent_container" 2>/dev/null || true)"
if [[ "$exit_code" != "2" ]]; then
  emit_failure_stage "gateway-terminal-exit-code"
  exit 1
fi
if ! "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx continuity; then
  emit_failure_stage "continuity-after-terminal-auth"
  exit 1
fi

# Keep the observation window longer than the configured initial retry delay.
# The terminal state must remain unchanged and restart:no must keep the Gateway
# down, proving the result did not accidentally enter the reconnect policy.
sleep 5
if ! terminal_auth_status; then
  emit_failure_stage "terminal-auth-status-stability"
  exit 1
fi
if ! terminal_attempt_after="$(read_terminal_attempt)"; then
  emit_failure_stage "terminal-auth-attempt-stability"
  exit 1
fi
if [[ "$terminal_attempt_after" != "$terminal_attempt_before" ]]; then
  emit_failure_stage "terminal-auth-retried"
  exit 1
fi
if "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx egress-gateway; then
  emit_failure_stage "gateway-restarted-terminal-auth"
  exit 1
fi

egress_logs_file="$tmp_dir/egress-gateway.log"
if ! "${compose[@]}" logs --no-color egress-gateway >"$egress_logs_file" 2>&1; then
  emit_failure_stage "egress-log-read"
  exit 1
fi
if ! file_excludes_generated_secrets "$egress_logs_file"; then
  emit_failure_stage "generated-secret-in-egress-log"
  exit 1
fi

echo "IRLight isolated legacy egress auth-terminal smoke passed."
