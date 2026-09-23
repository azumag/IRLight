#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
smoke_project="irlight-egress-isolated-publish-conflict-smoke-$$-$RANDOM"
tmp_dir="$(mktemp -d)"
override="$tmp_dir/egress-isolated-publish-conflict.override.yml"
target_config="$tmp_dir/mediamtx-conflict.yml"
secret_file="$tmp_dir/egress_url"
redaction_values_file="$tmp_dir/redaction-values"
stream_key="ci-isolated-conflict-$RANDOM"
path_name="conflict/$stream_key"
export EGRESS_SECRET_FILE="$secret_file"

printf '%s\n' "$stream_key" >"$redaction_values_file"
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
chmod 600 "$secret_file" "$redaction_values_file" "$target_config"

cat >"$override" <<EOF
services:
  egress-conflict-target:
    image: bluenviron/mediamtx:1.20.0
    restart: "no"
    volumes:
      - $target_config:/mediamtx.yml:ro

  conflict-holder:
    build:
      context: ./apps/continuity
      dockerfile: Dockerfile
    restart: "no"
    depends_on:
      - egress-conflict-target
    command:
      - /bin/sh
      - -c
      - |
        exec timeout --signal=INT --kill-after=5s 300s gst-launch-1.0 -q -e \\
          flvmux name=mux streamable=true ! \\
            rtmp2sink location='rtmp://egress-conflict-target:1935/$path_name' \\
          videotestsrc is-live=true pattern=black ! \\
            video/x-raw,width=640,height=360,framerate=15/1,format=I420 ! \\
            x264enc tune=zerolatency speed-preset=veryfast bitrate=600 key-int-max=30 bframes=0 ! \\
            video/x-h264,profile=main ! h264parse config-interval=-1 ! queue ! mux. \\
          audiotestsrc is-live=true wave=silence ! audioconvert ! audioresample ! \\
            audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=96000 ! aacparse ! queue ! mux.

  egress-gateway:
    build:
      context: ./apps/egress-gateway
      dockerfile: Dockerfile
    restart: "no"
    depends_on:
      - mediamtx
      - continuity
      - egress-conflict-target
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
      - $secret_file:/run/irlight/secrets/egress_url:ro
EOF
chmod 600 "$override"
unset stream_key path_name

compose=(docker compose -p "$smoke_project" -f "$repo_root/docker-compose.poc.yml" -f "$override")

emit_failure_stage() {
  printf '::error title=IRLight isolated egress publish-conflict smoke failure::stage=%s\n' "$1" >&2
}

redact_generated_secrets() {
  python3 -c '
from pathlib import Path
import sys
try:
    secrets = [value for value in Path(sys.argv[1]).read_bytes().splitlines() if value]
except Exception:
    raise SystemExit(1)
if not secrets:
    raise SystemExit(1)
data = sys.stdin.buffer.read()
for secret in secrets:
    data = data.replace(secret, b"<redacted>")
sys.stdout.buffer.write(data)
' "$redaction_values_file"
}

file_excludes_generated_secrets() {
  local candidate="$1"
  python3 -c '
from pathlib import Path
import sys
try:
    secrets = [value for value in Path(sys.argv[1]).read_bytes().splitlines() if value]
    data = Path(sys.argv[2]).read_bytes()
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if secrets and all(secret not in data for secret in secrets) else 1)
' "$redaction_values_file" "$candidate"
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
  return "$logs_rc"
}

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps (secret-safe) ---" >&2
    "${compose[@]}" ps -a >&2 || true
    echo "--- holder logs (redacted) ---" >&2
    emit_redacted_compose_logs conflict-holder 120 || true
    echo "--- gateway logs (redacted) ---" >&2
    emit_redacted_compose_logs egress-gateway 240 || true
    echo "--- target logs (redacted) ---" >&2
    emit_redacted_compose_logs egress-conflict-target 200 || true
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT

capture_compose_logs() {
  local service="$1"
  local output_file="$2"
  "${compose[@]}" logs --no-color "$service" >"$output_file" 2>&1
}

target_logs_contain_path_marker() {
  local marker_kind="$1"
  local output_file="$2"
  if ! capture_compose_logs egress-conflict-target "$output_file"; then
    return 2
  fi
  python3 -c '
from pathlib import Path
import sys
try:
    secrets = [value for value in Path(sys.argv[1]).read_bytes().splitlines() if value]
    data = Path(sys.argv[3]).read_bytes()
except Exception:
    raise SystemExit(2)
if len(secrets) != 1:
    raise SystemExit(2)
path = b"conflict/" + secrets[0]
quote = bytes([39])
markers = {
    "publishing": b"is publishing to path " + quote + path + quote,
    "conflict": b"someone is already publishing to path " + quote + path + quote,
}
marker = markers.get(sys.argv[2])
if marker is None:
    raise SystemExit(2)
raise SystemExit(0 if marker in data else 1)
' "$redaction_values_file" "$marker_kind" "$output_file"
}

read_egress_status() {
  "${compose[@]}" exec -T continuity cat /state/egress.json 2>/dev/null || true
}

terminal_conflict_status() {
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
        (reason == "PUBLISH_CONFLICT" and status == "AUTH_FAILED")
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

wait_terminal_conflict_status() {
  local timeout="${1:-60}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if terminal_conflict_status; then
      return 0
    fi
    sleep 1
  done
  terminal_conflict_status
}

wait_for_target_listener() {
  local timeout="${1:-30}"
  local deadline=$((SECONDS + timeout))
  local listener_logs="$tmp_dir/egress-conflict-target.listener.log"
  while (( SECONDS < deadline )); do
    if capture_compose_logs egress-conflict-target "$listener_logs" && grep -Fq 'started with listener on :1935' "$listener_logs"; then
      return 0
    fi
    if ! "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx egress-conflict-target; then
      return 1
    fi
    sleep 1
  done
  return 1
}

if ! "${compose[@]}" config >/dev/null; then
  emit_failure_stage "compose-config"
  exit 1
fi
if ! "${compose[@]}" build continuity control-ui node-agent conflict-holder egress-gateway; then
  emit_failure_stage "compose-build"
  exit 1
fi
if ! "${compose[@]}" up -d egress-conflict-target; then
  emit_failure_stage "target-up"
  exit 1
fi
if ! wait_for_target_listener 30; then
  emit_failure_stage "target-listener"
  exit 1
fi
if ! "${compose[@]}" up -d mediamtx continuity control-ui node-agent conflict-holder; then
  emit_failure_stage "holder-up"
  exit 1
fi

holder_poll_logs="$tmp_dir/egress-conflict-target.holder-poll.log"
for _ in $(seq 1 20); do
  if ! "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx conflict-holder; then
    emit_failure_stage "holder-exited"
    exit 1
  fi
  if target_logs_contain_path_marker publishing "$holder_poll_logs"; then
    break
  fi
  sleep 1
done
holder_final_logs="$tmp_dir/egress-conflict-target.holder-final.log"
if ! target_logs_contain_path_marker publishing "$holder_final_logs"; then
  emit_failure_stage "holder-not-publishing"
  exit 1
fi

if ! "${compose[@]}" up -d egress-gateway; then
  emit_failure_stage "gateway-up"
  exit 1
fi
parent_container="$("${compose[@]}" ps -aq egress-gateway)"
if [[ -z "$parent_container" ]]; then
  emit_failure_stage "gateway-container-id"
  exit 1
fi
container_env="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$parent_container" 2>/dev/null || true)"
if ! grep -Fxq 'EGRESS_RTMP_SINK_FACTORY=rtmpsink' <<<"$container_env"; then
  emit_failure_stage "legacy-sink-not-configured"
  exit 1
fi
if ! grep -Fxq 'EGRESS_LEGACY_PROCESS_ISOLATION_CANARY=1' <<<"$container_env"; then
  emit_failure_stage "process-isolation-canary-not-configured"
  exit 1
fi
if ! wait_terminal_conflict_status 60; then
  emit_failure_stage "terminal-conflict-status"
  exit 1
fi
if ! terminal_attempt_before="$(read_terminal_attempt)"; then
  emit_failure_stage "terminal-conflict-attempt"
  exit 1
fi

conflict_evidence_logs="$tmp_dir/egress-conflict-target.conflict-evidence.log"
if ! target_logs_contain_path_marker conflict "$conflict_evidence_logs"; then
  emit_failure_stage "target-conflict-evidence"
  exit 1
fi

for _ in $(seq 1 10); do
  if ! "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx egress-gateway; then
    break
  fi
  sleep 1
done
if "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx egress-gateway; then
  emit_failure_stage "gateway-retried-terminal-conflict"
  exit 1
fi
exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$parent_container" 2>/dev/null || true)"
if [[ "$exit_code" != "2" ]]; then
  emit_failure_stage "gateway-terminal-exit-code"
  exit 1
fi
if ! "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx conflict-holder; then
  emit_failure_stage "holder-evicted"
  exit 1
fi
if ! "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx continuity; then
  emit_failure_stage "continuity-after-terminal-conflict"
  exit 1
fi

# Observe longer than the configured retry delay. A terminal child result must
# keep the same attempt and leave the parent down rather than entering retry.
sleep 5
if ! terminal_conflict_status; then
  emit_failure_stage "terminal-conflict-status-stability"
  exit 1
fi
if ! terminal_attempt_after="$(read_terminal_attempt)"; then
  emit_failure_stage "terminal-conflict-attempt-stability"
  exit 1
fi
if [[ "$terminal_attempt_after" != "$terminal_attempt_before" ]]; then
  emit_failure_stage "terminal-conflict-retried"
  exit 1
fi
if "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx egress-gateway; then
  emit_failure_stage "gateway-restarted-terminal-conflict"
  exit 1
fi

status_file="$tmp_dir/final-status.json"
read_egress_status >"$status_file"
if ! file_excludes_generated_secrets "$status_file"; then
  emit_failure_stage "generated-secret-in-status"
  exit 1
fi
gateway_logs_file="$tmp_dir/egress-gateway.log"
if ! capture_compose_logs egress-gateway "$gateway_logs_file"; then
  emit_failure_stage "gateway-log-read"
  exit 1
fi
if ! file_excludes_generated_secrets "$gateway_logs_file"; then
  emit_failure_stage "generated-secret-in-gateway-log"
  exit 1
fi

echo "IRLight isolated legacy egress publish-conflict smoke passed."
