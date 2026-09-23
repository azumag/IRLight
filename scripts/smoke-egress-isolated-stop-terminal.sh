#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
smoke_project="irlight-egress-isolated-stop-terminal-smoke-$$-$RANDOM"
tmp_dir="$(mktemp -d)"
override="$tmp_dir/egress-isolated-stop-terminal.override.yml"
secret_file="$tmp_dir/egress_url"
stream_key="ci-egress-isolated-stop-secret-$RANDOM"
export EGRESS_SECRET_FILE="$secret_file"

cat >"$secret_file" <<EOF
rtmp://egress-target:1935/live/$stream_key
EOF
chmod 600 "$secret_file"
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
      EGRESS_ISOLATED_TEARDOWN_TIMEOUT_SECONDS: "1"
      EGRESS_ISOLATED_TERMINATE_TIMEOUT_SECONDS: "1"
      EGRESS_ISOLATED_KILL_TIMEOUT_SECONDS: "1"
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
  printf '::error title=IRLight isolated egress stop smoke failure::stage=%s\n' "$1" >&2
}

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps (secret-safe) ---" >&2
    "${compose[@]}" ps -a >&2 || true
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT

read_egress_status() {
  # Continuity shares the state volume and stays alive after the Gateway exits,
  # so it can observe the final STOPPED status without restarting the Gateway.
  "${compose[@]}" exec -T continuity cat /state/egress.json 2>/dev/null || true
}

status_matches() {
  local expected_status="$1"
  local expected_reason="${2:-}"
  read_egress_status | python3 -c '
import json
import sys
expected_status, expected_reason = sys.argv[1], sys.argv[2]
try:
    value = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
if value.get("status") != expected_status:
    raise SystemExit(1)
if expected_reason and value.get("reason_code") != expected_reason:
    raise SystemExit(1)
raise SystemExit(0)
' "$expected_status" "$expected_reason" 2>/dev/null
}

wait_egress_status() {
  local expected_status="$1"
  local expected_reason="${2:-}"
  local timeout="${3:-45}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if status_matches "$expected_status" "$expected_reason"; then
      return 0
    fi
    sleep 1
  done
  status_matches "$expected_status" "$expected_reason"
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
if ! wait_egress_status CONNECTED "" 60; then
  emit_failure_stage "egress-connected-initial"
  exit 1
fi

parent_container="$("${compose[@]}" ps -q egress-gateway)"
if [[ -z "$parent_container" ]]; then
  emit_failure_stage "gateway-container-id-initial"
  exit 1
fi
if ! initial_identity="$(wait_attempt_identity 20)"; then
  emit_failure_stage "attempt-child-initial"
  exit 1
fi
if ! assert_attempt_secret_free "$initial_identity"; then
  emit_failure_stage "attempt-child-secret-boundary"
  exit 1
fi

# Stop while the isolated child is actively connected. With the canary timers
# bounded to one second each, a cooperative stop or forced fence must complete
# before Docker's outer eight-second grace period. A timeout-induced SIGKILL
# would leave a non-zero container exit code and no USER_STOPPED status.
if ! "${compose[@]}" stop -t 8 egress-gateway >/dev/null; then
  emit_failure_stage "gateway-stop"
  exit 1
fi
if ! wait_egress_status STOPPED USER_STOPPED 5; then
  emit_failure_stage "stopped-user-stopped"
  exit 1
fi

parent_container_after="$("${compose[@]}" ps -aq egress-gateway)"
if [[ "$parent_container_after" != "$parent_container" ]]; then
  emit_failure_stage "gateway-container-replaced"
  exit 1
fi
if "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx egress-gateway; then
  emit_failure_stage "gateway-still-running"
  exit 1
fi
if ! "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx continuity; then
  emit_failure_stage "continuity-after-stop"
  exit 1
fi

exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$parent_container" 2>/dev/null || true)"
if [[ "$exit_code" != "0" ]]; then
  emit_failure_stage "gateway-graceful-exit"
  exit 1
fi

# restart: "no" is part of the terminal/user-stop contract. An online target
# must not resurrect the Gateway after an explicit user stop.
sleep 3
if "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx egress-gateway; then
  emit_failure_stage "gateway-restarted-after-user-stop"
  exit 1
fi
if ! status_matches STOPPED USER_STOPPED; then
  emit_failure_stage "stopped-status-stable"
  exit 1
fi

echo "IRLight isolated legacy egress user-stop smoke passed."
