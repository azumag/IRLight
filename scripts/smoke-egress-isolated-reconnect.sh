#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
smoke_project="irlight-egress-isolated-reconnect-smoke-$$-$RANDOM"
tmp_dir="$(mktemp -d)"
override="$tmp_dir/egress-isolated-reconnect.override.yml"
secret_file="$tmp_dir/egress_url"
stream_key_file="$tmp_dir/stream_key"
stream_key="ci-egress-isolated-secret-$RANDOM"
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
      - ${EGRESS_SECRET_FILE}:/run/irlight/secrets/egress_url:ro
YAML

compose=(docker compose -p "$smoke_project" -f "$repo_root/docker-compose.poc.yml" -f "$override")

emit_failure_stage() {
  printf '::error title=IRLight isolated egress smoke failure::stage=%s\n' "$1" >&2
}

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps (secret-safe) ---" >&2
    "${compose[@]}" ps >&2 || true
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT

read_egress_status() {
  "${compose[@]}" exec -T egress-gateway cat /state/egress.json 2>/dev/null || true
}

egress_status_matches() {
  local expected="$1"
  read_egress_status | python3 -c '
import json
import sys
expected = sys.argv[1]
try:
    value = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if value.get("status") == expected else 1)
' "$expected" 2>/dev/null
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
  egress_status_matches "$expected"
}

wait_gateway_running() {
  local timeout="${1:-30}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx egress-gateway; then
      return 0
    fi
    sleep 1
  done
  return 1
}

find_attempt_identity() {
  "${compose[@]}" exec -T egress-gateway python3 -c '
from pathlib import Path

matches = []
for proc in Path("/proc").iterdir():
    if not proc.name.isdigit():
        continue
    try:
        cmdline = (proc / "cmdline").read_bytes()
        status = (proc / "status").read_text(encoding="utf-8", errors="replace")
        stat = (proc / "stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        continue
    ppid = None
    for line in status.splitlines():
        if line.startswith("PPid:"):
            ppid = int(line.split()[1])
            break
    if ppid != 1 or b"spawn_main" not in cmdline:
        continue
    # /proc/<pid>/stat field 22 is process start time. Pairing it with PID
    # avoids treating rapid PID reuse as the old attempt still being alive.
    fields = stat.split()
    if len(fields) < 22:
        continue
    matches.append((int(proc.name), fields[21]))
if len(matches) != 1:
    raise SystemExit(1)
pid, start_time = matches[0]
print(f"{pid}:{start_time}")
' 2>/dev/null
}

wait_attempt_identity() {
  local timeout="${1:-20}"
  local deadline=$((SECONDS + timeout))
  local identity
  while (( SECONDS < deadline )); do
    if identity="$(find_attempt_identity)" && [[ -n "$identity" ]]; then
      printf '%s' "$identity"
      return 0
    fi
    sleep 1
  done
  return 1
}

attempt_identity_alive() {
  local identity="$1"
  local pid="${identity%%:*}"
  local start_time="${identity#*:}"
  "${compose[@]}" exec -T egress-gateway python3 -c '
from pathlib import Path
import sys
pid, expected_start = sys.argv[1], sys.argv[2]
try:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace").split()
except (FileNotFoundError, PermissionError, ProcessLookupError):
    raise SystemExit(1)
raise SystemExit(0 if len(fields) >= 22 and fields[21] == expected_start else 1)
' "$pid" "$start_time" >/dev/null 2>&1
}

assert_attempt_secret_free() {
  local identity="$1"
  local pid="${identity%%:*}"
  "${compose[@]}" exec -T egress-gateway python3 -c '
from pathlib import Path
import sys
pid = sys.argv[1]
secret_url = Path("/run/irlight/secrets/egress_url").read_bytes().strip()
stream_key = secret_url.rsplit(b"/", 1)[-1]
if not secret_url or not stream_key:
    raise SystemExit(2)
try:
    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    environ = Path(f"/proc/{pid}/environ").read_bytes()
except (FileNotFoundError, PermissionError, ProcessLookupError):
    raise SystemExit(3)
if secret_url in cmdline or stream_key in cmdline:
    raise SystemExit(4)
if secret_url in environ or stream_key in environ:
    raise SystemExit(5)
' "$pid" >/dev/null 2>&1
}

if ! "${compose[@]}" config >/dev/null; then
  emit_failure_stage "compose-config"
  exit 1
fi
if ! "${compose[@]}" up -d --build; then
  emit_failure_stage "compose-up"
  exit 1
fi
if ! wait_gateway_running 30; then
  emit_failure_stage "gateway-running-initial"
  exit 1
fi
if ! wait_egress_status CONNECTED 60; then
  emit_failure_stage "egress-connected-initial"
  exit 1
fi

parent_container_before="$("${compose[@]}" ps -q egress-gateway)"
if [[ -z "$parent_container_before" ]]; then
  emit_failure_stage "gateway-container-id-initial"
  exit 1
fi
if ! initial_identity="$(wait_attempt_identity 20)"; then
  emit_failure_stage "attempt-child-initial"
  exit 1
fi
if ! assert_attempt_secret_free "$initial_identity"; then
  emit_failure_stage "attempt-child-secret-boundary-initial"
  exit 1
fi

if ! "${compose[@]}" stop egress-target >/dev/null; then
  emit_failure_stage "target-stop"
  exit 1
fi
if ! wait_egress_status RECONNECTING 45; then
  emit_failure_stage "egress-reconnecting"
  exit 1
fi

parent_container_after_outage="$("${compose[@]}" ps -q egress-gateway)"
if [[ "$parent_container_after_outage" != "$parent_container_before" ]]; then
  emit_failure_stage "parent-gateway-restarted"
  exit 1
fi
if ! "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx egress-gateway; then
  emit_failure_stage "parent-gateway-not-running"
  exit 1
fi
if attempt_identity_alive "$initial_identity"; then
  emit_failure_stage "old-attempt-not-reaped"
  exit 1
fi
if ! "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx continuity; then
  emit_failure_stage "continuity-during-outage"
  exit 1
fi

if ! "${compose[@]}" start egress-target >/dev/null; then
  emit_failure_stage "target-start"
  exit 1
fi
if ! wait_egress_status CONNECTED 60; then
  emit_failure_stage "egress-connected-recovery"
  exit 1
fi
if ! recovered_identity="$(wait_attempt_identity 20)"; then
  emit_failure_stage "attempt-child-recovery"
  exit 1
fi
if [[ "$recovered_identity" == "$initial_identity" ]]; then
  emit_failure_stage "attempt-child-not-replaced"
  exit 1
fi
if ! assert_attempt_secret_free "$recovered_identity"; then
  emit_failure_stage "attempt-child-secret-boundary-recovery"
  exit 1
fi

parent_container_after_recovery="$("${compose[@]}" ps -q egress-gateway)"
if [[ "$parent_container_after_recovery" != "$parent_container_before" ]]; then
  emit_failure_stage "parent-gateway-restarted-on-recovery"
  exit 1
fi
if ! "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx continuity; then
  emit_failure_stage "continuity-after-recovery"
  exit 1
fi

echo "IRLight isolated legacy egress reconnect smoke passed."
