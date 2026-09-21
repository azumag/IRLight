#!/usr/bin/env bash
set -euo pipefail
umask 077
export EGRESS_RTMP_SINK_FACTORY="${EGRESS_RTMP_SINK_FACTORY:-rtmpsink}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
smoke_project="irlight-egress-reconnect-smoke-$$-$RANDOM"
tmp_dir="$(mktemp -d)"
override="$tmp_dir/egress-reconnect.override.yml"
secret_file="$tmp_dir/egress_url"
stream_key_file="$tmp_dir/stream_key"
stream_key="ci-egress-secret-$RANDOM"
export EGRESS_SECRET_FILE="$secret_file"

printf '%s' "$stream_key" >"$stream_key_file"
cat >"$secret_file" <<EOF
rtmp://egress-target:1935/live/$stream_key
EOF
chmod 600 "$secret_file" "$stream_key_file"
unset stream_key

cat >"$override" <<'YAML'
services:
  egress-target:
    image: bluenviron/mediamtx:1.20.0
    restart: "no"
    environment:
      MTX_API: "yes"
      MTX_APIADDRESS: ":9997"
      # MediaMTX grants API access to localhost only by default. This target is
      # isolated inside the smoke-test Compose network, so extend the existing
      # anonymous test user with API permission for cross-container inspection.
      MTX_AUTHINTERNALUSERS_0_PERMISSIONS_3_ACTION: "api"

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
      # This target deliberately lives on the isolated Compose RFC1918 network.
      # Production keeps the runtime DNS guard fail-closed by default.
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
      - ${EGRESS_SECRET_FILE}:/run/irlight/secrets/egress_url:ro
YAML

compose=(docker compose -p "$smoke_project" -f "$repo_root/docker-compose.poc.yml" -f "$override")

emit_failure_stage() {
  local stage="$1"
  printf '::error title=IRLight docker smoke failure::stage=%s\n' "$stage" >&2
}

redact_stream_key() {
  python3 -c '
from pathlib import Path
import sys
secret = Path(sys.argv[1]).read_text(encoding="utf-8")
data = sys.stdin.read()
sys.stdout.write(data.replace(secret, "<redacted>"))
' "$stream_key_file"
}

stdin_excludes_stream_key() {
  python3 -c '
from pathlib import Path
import sys
secret = Path(sys.argv[1]).read_bytes()
data = sys.stdin.buffer.read()
raise SystemExit(0 if secret and secret not in data else 1)
' "$stream_key_file"
}

file_excludes_stream_key() {
  local candidate="$1"
  python3 -c '
from pathlib import Path
import sys
secret = Path(sys.argv[1]).read_bytes()
data = Path(sys.argv[2]).read_bytes()
raise SystemExit(0 if secret and secret not in data else 1)
' "$stream_key_file" "$candidate"
}

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps >&2 || true
    echo "--- continuity logs ---" >&2
    "${compose[@]}" logs --no-color --tail=120 continuity 2>&1 | redact_stream_key >&2 || true
    echo "--- egress gateway logs ---" >&2
    "${compose[@]}" logs --no-color --tail=160 egress-gateway 2>&1 | redact_stream_key >&2 || true
    echo "--- target logs ---" >&2
    "${compose[@]}" logs --no-color --tail=120 egress-target 2>&1 | redact_stream_key >&2 || true
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT

target_api() {
  "${compose[@]}" exec -T egress-gateway python3 -c '
import sys
import urllib.request
try:
    with urllib.request.urlopen(
        "http://egress-target:9997/v3/paths/list?itemsPerPage=100", timeout=3
    ) as response:
        sys.stdout.write(response.read().decode("utf-8"))
except Exception:
    raise SystemExit(1)
' 2>/dev/null
}

wait_target_api() {
  local timeout="${1:-60}"
  local deadline=$((SECONDS + timeout))
  until target_api >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      # One last observation avoids a false timeout when readiness changes on
      # the deadline boundary after the preceding failed probe.
      if target_api >/dev/null 2>&1; then
        return 0
      fi
      echo "target Control API did not become ready" >&2
      return 1
    fi
    sleep 1
  done
}

read_egress_status() {
  "${compose[@]}" exec -T egress-gateway cat /state/egress.json 2>/dev/null || true
}

emit_reconnect_timeout_evidence() {
  local payload gateway_state evidence
  payload="$(read_egress_status)"
  gateway_state="not-running"
  if "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx egress-gateway; then
    gateway_state="running"
  fi

  if ! evidence="$(python3 -c '
import json
import sys

allowed_statuses = {
    "STARTING", "CONNECTED", "RECONNECTING", "AUTH_FAILED", "FAILED", "STOPPED"
}
allowed_reasons = {
    "AUTH_FAILED", "PUBLISH_CONFLICT", "PUBLISH_REJECTED", "LOCAL_PIPELINE_FAILED",
    "TLS_FAILED", "DNS_FAILED", "TIMEOUT", "UNREACHABLE", "UPSTREAM_UNAVAILABLE",
    "UPSTREAM_EOS", "EGRESS_PIPELINE_FAILED", "RETRY_EXHAUSTED", "USER_STOPPED",
    "SECRET_UNAVAILABLE", "DESTINATION_UNSAFE"
}
gateway = sys.argv[1] if sys.argv[1] in {"running", "not-running"} else "unknown"
try:
    value = json.load(sys.stdin)
except Exception:
    raise SystemExit(2)
status_value = value.get("status")
status = status_value if status_value in allowed_statuses else "OTHER"
reason_value = value.get("reason_code")
if reason_value is None:
    reason = "NONE"
elif reason_value in allowed_reasons:
    reason = reason_value
else:
    reason = "OTHER"
attempt_value = value.get("attempt")
attempt = (
    str(attempt_value)
    if isinstance(attempt_value, int) and not isinstance(attempt_value, bool) and attempt_value >= 0
    else "-"
)
connected_value = value.get("connected")
connected = "yes" if connected_value is True else "no" if connected_value is False else "-"
next_retry_present = "yes" if value.get("next_retry_at") is not None else "no"
print(
    "IRLIGHT_EGRESS_RECONNECT_EVIDENCE "
    f"status={status} reason={reason} attempt={attempt} connected={connected} "
    f"next_retry_present={next_retry_present} gateway={gateway}"
)
' "$gateway_state" <<<"$payload" 2>/dev/null)"; then
    evidence="IRLIGHT_EGRESS_RECONNECT_EVIDENCE status=UNREADABLE reason=UNREADABLE attempt=- connected=- next_retry_present=- gateway=$gateway_state"
  fi

  printf '%s\n' "$evidence" >&2
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
      echo
      echo '#### Egress reconnect timeout evidence'
      echo
      printf '`%s`\n' "$evidence"
    } >>"$GITHUB_STEP_SUMMARY"
  fi
}

egress_status_matches() {
  local expected="$1"
  local payload
  payload="$(read_egress_status)"
  python3 -c '
import json,sys
expected=sys.argv[1]
try:
    value=json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if value.get("status") == expected else 1)
' "$expected" <<<"$payload" 2>/dev/null
}

wait_egress_status() {
  local expected="$1"
  local timeout="${2:-45}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if egress_status_matches "$expected"; then
      return 0
    fi
    sleep 1
  done
  # The status file can be atomically replaced on the timeout boundary between
  # the last poll and the deadline check. Observe it once more before failing.
  if egress_status_matches "$expected"; then
    return 0
  fi
  local safe_last
  safe_last="$(read_egress_status | redact_stream_key 2>/dev/null || true)"
  echo "egress status did not become $expected; last=$safe_last" >&2
  return 1
}

target_path_ready() {
  local payload
  payload="$(target_api 2>/dev/null || true)"
  python3 -c '
from pathlib import Path
import json,sys
secret = Path(sys.argv[1]).read_text(encoding="utf-8")
name = f"live/{secret}"
try:
    value=json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
items=value.get("items", [])
raise SystemExit(0 if any(item.get("name") == name and item.get("ready") is True for item in items) else 1)
' "$stream_key_file" <<<"$payload" 2>/dev/null
}

wait_target_path() {
  local timeout="${1:-45}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if target_path_ready; then
      return 0
    fi
    sleep 1
  done
  # Match wait_egress_status's final-observation rule for MediaMTX readiness.
  if target_path_ready; then
    return 0
  fi
  echo "target did not receive the expected test stream; API snapshot withheld because it can contain the generated stream key" >&2
  return 1
}

# The generated project must never borrow or tear down a developer stack. If a
# fixed host port is occupied, let `up` fail rather than preempting that owner.
if ! "${compose[@]}" config >/dev/null; then
  emit_failure_stage "compose-config"
  exit 1
fi
# Continuity now receives its authenticated local-media URIs from Node Agent
# tmpfs files. Start the complete PoC dependency chain so the test exercises
# production-equivalent secret delivery instead of bypassing it.
if ! "${compose[@]}" up -d --build; then
  emit_failure_stage "compose-up"
  exit 1
fi
if ! wait_target_api 60; then
  emit_failure_stage "target-api-initial"
  exit 1
fi
if ! wait_egress_status CONNECTED 60; then
  emit_failure_stage "egress-connected-initial"
  exit 1
fi
if ! wait_target_path 60; then
  emit_failure_stage "target-path-initial"
  exit 1
fi

status_payload="$(read_egress_status)"
if ! stdin_excludes_stream_key <<<"$status_payload"; then
  echo "egress status stream-key check failed closed" >&2
  emit_failure_stage "secret-redaction-status"
  exit 1
fi

egress_logs_file="$tmp_dir/egress-gateway.log"
if ! "${compose[@]}" logs --no-color egress-gateway >"$egress_logs_file"; then
  echo "failed to read egress gateway logs for secret redaction check" >&2
  emit_failure_stage "secret-redaction-logs-read"
  exit 1
fi
if ! file_excludes_stream_key "$egress_logs_file"; then
  echo "egress log stream-key check failed closed" >&2
  emit_failure_stage "secret-redaction-logs"
  exit 1
fi

# Simulate a remote RTMP outage. The Egress Gateway must reconnect on its own;
# Continuity must keep publishing the local output/relay stream throughout.
if ! "${compose[@]}" stop egress-target >/dev/null; then
  emit_failure_stage "target-stop"
  exit 1
fi
if ! wait_egress_status RECONNECTING 45; then
  emit_reconnect_timeout_evidence
  emit_failure_stage "egress-reconnecting"
  exit 1
fi
if ! "${compose[@]}" ps --status running --services | grep -qx continuity; then
  echo "continuity stopped when the external destination went down" >&2
  emit_failure_stage "continuity-during-outage"
  exit 1
fi

if ! "${compose[@]}" start egress-target >/dev/null; then
  emit_failure_stage "target-start"
  exit 1
fi
if ! wait_target_api 45; then
  emit_failure_stage "target-api-recovery"
  exit 1
fi
if ! wait_egress_status CONNECTED 60; then
  emit_failure_stage "egress-connected-recovery"
  exit 1
fi
if ! wait_target_path 60; then
  emit_failure_stage "target-path-recovery"
  exit 1
fi

if ! "${compose[@]}" ps --status running --services | grep -qx continuity; then
  echo "continuity is not running after egress recovery" >&2
  emit_failure_stage "continuity-after-recovery"
  exit 1
fi

echo "IRLight egress reconnect smoke passed."
