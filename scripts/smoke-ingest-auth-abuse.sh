#!/usr/bin/env bash
set -euo pipefail
umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib/node-admin.sh"

smoke_project="irlight-ingest-auth-abuse-smoke-$$-$RANDOM"
tmp_dir="$(mktemp -d)"
override="$tmp_dir/auth-abuse.override.yml"
cat >"$override" <<'YAML'
services:
  control-ui:
    environment:
      NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT: "1"
  node-agent:
    environment:
      NODE_PROVIDER_SERVER_ID: ${ASSIGNED_PROVIDER_SERVER_ID:-unassigned-provider}
      NODE_BOOT_ID: auth-abuse-smoke-boot
YAML

compose=(docker compose -p "$smoke_project" -f docker-compose.poc.yml -f "$override")
base_url="${BASE_URL:-http://127.0.0.1:8080}"
source_ip="203.0.113.50"
wrong_secret="abuse-secret-must-never-persist"
cookie_jar="$tmp_dir/cookies.txt"
email="auth-abuse-$(date +%s)-$RANDOM@example.invalid"
password="SmokePassword123!"
csrf=""
session_id=""
ingest_username=""

export IRLIGHT_INGEST_AUTH_FAILURE_WINDOW_SECONDS=60
export IRLIGHT_INGEST_AUTH_MAX_FAILURES_PER_CREDENTIAL=3
export IRLIGHT_INGEST_AUTH_MAX_FAILURES_PER_IP=20
export IRLIGHT_INGEST_AUTH_LOCKOUT_SECONDS=30

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps >&2 || true
    echo "--- node-agent logs ---" >&2
    "${compose[@]}" logs --no-color --tail=150 node-agent >&2 || true
    echo "--- control-ui logs ---" >&2
    "${compose[@]}" logs --no-color --tail=150 control-ui >&2 || true
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT

wait_http() {
  local deadline=$((SECONDS + ${2:-60}))
  until curl -fsS --max-time 3 "$1" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "HTTP endpoint did not become ready: $1" >&2
      return 1
    fi
    sleep 1
  done
}

wait_assigned_node() {
  local deadline=$((SECONDS + 45))
  while (( SECONDS < deadline )); do
    payload="$(node_admin_curl -fsS --max-time 3 "$base_url/internal/nodes" 2>/dev/null || true)"
    if python3 -c '
import json,sys
session_id=sys.argv[1]
d=json.load(sys.stdin)
raise SystemExit(0 if any(n.get("session_assigned") is True and n.get("session_id") == session_id for n in d.get("nodes", {}).values()) else 1)
' "$session_id" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "node-agent did not bind to Session" >&2
  return 1
}

login() {
  local response
  rm -f "$cookie_jar"
  response="$(curl -fsS --max-time 10 -c "$cookie_jar" -X POST "$base_url/v1/auth/login" \
    -H 'Content-Type: application/json' \
    --data "{\"email\":\"$email\",\"password\":\"$password\"}")"
  csrf="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])' <<<"$response")"
}

auth_response() {
  "${compose[@]}" exec -T \
    -e AUTH_USER="$ingest_username" \
    -e AUTH_SECRET="$wrong_secret" \
    -e AUTH_SOURCE_IP="$source_ip" \
    node-agent python3 -c '
import json, os, urllib.error, urllib.request
payload = {
    "user": os.environ["AUTH_USER"],
    "password": os.environ["AUTH_SECRET"],
    "token": "",
    "ip": os.environ["AUTH_SOURCE_IP"],
    "action": "publish",
    "path": "live/input",
    "protocol": "rtmp",
    "id": "abuse-smoke",
    "query": "",
    "userAgent": "auth-abuse-smoke",
}
request = urllib.request.Request(
    "http://127.0.0.1:8090/auth",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        result = {"status": response.status, "retry_after": response.headers.get("Retry-After")}
except urllib.error.HTTPError as exc:
    try:
        result = {"status": exc.code, "retry_after": exc.headers.get("Retry-After") if exc.headers else None}
    finally:
        exc.close()
print(json.dumps(result, separators=(",", ":")))
'
}

response_status() {
  python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
}

response_retry_after() {
  python3 -c 'import json,sys; print(json.load(sys.stdin).get("retry_after") or "")'
}

# The generated project must never borrow or tear down a developer stack. If a
# fixed host port is occupied, let `up` fail rather than preempting that owner.
"${compose[@]}" config >/dev/null
# Allocate the Session first, then bootstrap a Node whose bearer is formally
# bound to that Session. The abuse guard must be tested behind the same node
# authentication boundary used in production.
"${compose[@]}" up -d --build control-ui
wait_http "$base_url/healthz" 60

curl -fsS --max-time 10 -X POST "$base_url/v1/auth/register" \
  -H 'Content-Type: application/json' \
  --data "{\"email\":\"$email\",\"password\":\"$password\",\"display_name\":\"Auth Abuse Smoke\"}" >/dev/null
login

session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
prepared="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/prepare" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  -H "Idempotency-Key: auth-abuse-$session_id" \
  --data '{"environment":"dev"}')"
provider_server_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["provider_server_id"])' <<<"$prepared")"
export ASSIGNED_PROVIDER_SERVER_ID="$provider_server_id"
"${compose[@]}" up -d --build node-agent
wait_assigned_node

credential="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/ingest-credentials" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  --data '{"protocols":["rtmp"],"ttl_seconds":3600}')"
ingest_username="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])' <<<"$credential")"

for attempt in 1 2; do
  response="$(auth_response)"
  status="$(response_status <<<"$response")"
  if [[ "$status" != "401" ]]; then
    echo "attempt $attempt expected HTTP 401, got $status" >&2
    exit 1
  fi
done

response="$(auth_response)"
status="$(response_status <<<"$response")"
if [[ "$status" != "429" ]]; then
  echo "threshold attempt expected HTTP 429, got $status" >&2
  exit 1
fi
retry_after="$(response_retry_after <<<"$response")"
if [[ -z "$retry_after" ]]; then
  echo "HTTP 429 did not include Retry-After" >&2
  exit 1
fi

response="$(auth_response)"
status="$(response_status <<<"$response")"
if [[ "$status" != "429" ]]; then
  echo "locked attempt expected HTTP 429, got $status" >&2
  exit 1
fi

state="$("${compose[@]}" exec -T control-ui cat /state/ingest_auth_guard.json)"
if grep -Fq "$wrong_secret" <<<"$state"; then
  echo "raw ingest secret leaked into auth guard state" >&2
  exit 1
fi
python3 -c '
import json, sys
state = json.load(sys.stdin)
types = [event.get("type") for event in state.get("events", [])]
required = {"ingest.auth_failed", "ingest.auth_locked", "ingest.auth_blocked"}
missing = sorted(required.difference(types))
if missing:
    raise SystemExit(f"missing auth guard events: {missing}")
if len(state.get("buckets", {})) > 4096:
    raise SystemExit("auth guard bucket bound exceeded")
' <<<"$state"

echo "IRLight ingest auth abuse smoke passed."
