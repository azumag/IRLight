#!/usr/bin/env bash
set -euo pipefail

tmp_dir="$(mktemp -d)"
override="$tmp_dir/egress-secret.override.yml"
cookie_jar="$tmp_dir/cookies.txt"
base_url="${BASE_URL:-http://127.0.0.1:8080}"
email="egress-secret-$(date +%s)-$RANDOM@example.invalid"
password='SmokePassword123!'
bootstrap_token='egress-secret-node-token'
secret_ref="egress/smoke-$RANDOM"
stream_key="delivery-secret-$RANDOM/with?chars"

cat >"$override" <<'YAML'
services:
  control-ui:
    environment:
      NODE_BOOTSTRAP_TOKENS: egress-secret-node-token
      NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT: "1"
      IRLIGHT_REQUIRE_DESTINATION: "1"
  node-agent:
    environment:
      NODE_BOOTSTRAP_TOKEN: egress-secret-node-token
      NODE_PROVIDER_SERVER_ID: ${ASSIGNED_PROVIDER_SERVER_ID:-unassigned-provider}
      NODE_BOOT_ID: egress-secret-boot
      NODE_HEARTBEAT_INTERVAL: "2"
YAML

compose=(docker compose -f docker-compose.poc.yml -f "$override")

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps >&2 || true
    echo "--- control logs ---" >&2
    "${compose[@]}" logs --no-color --tail=140 control-ui >&2 || true
    echo "--- node-agent logs ---" >&2
    "${compose[@]}" logs --no-color --tail=140 node-agent >&2 || true
  fi
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

wait_assigned_node() {
  local session_id="$1"
  local timeout="${2:-45}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    payload="$(curl -fsS --max-time 5 "$base_url/internal/nodes" 2>/dev/null || true)"
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
  echo "Node did not bind to destination Session" >&2
  return 1
}

wait_secret_file() {
  local timeout="${1:-30}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if "${compose[@]}" exec -T node-agent test -s /tmp/irlight-node-secrets/egress_url >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Node Agent did not materialize egress_url secret file" >&2
  return 1
}

"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
# Keep Node Agent stopped until a user Session has allocated its provider server.
"${compose[@]}" up -d --build control-ui
wait_http "$base_url/healthz" 60

curl -fsS --max-time 10 -X POST "$base_url/v1/auth/register" \
  -H 'Content-Type: application/json' \
  --data "{\"email\":\"$email\",\"password\":\"$password\",\"display_name\":\"Egress Secret Smoke\"}" >/dev/null
login

destination_payload="$(python3 -c '
import json,sys
print(json.dumps({
  "type":"rtmp",
  "display_name":"Encrypted egress smoke",
  "server_url":"rtmp://mediamtx:1935/output/relay/{stream_key}",
  "secret_ref":sys.argv[1],
}))
' "$secret_ref")"
destination="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST "$base_url/v1/destinations" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  --data "$destination_payload")"
destination_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$destination")"

curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/destinations/$destination_id/verify" \
  -H "X-CSRF-Token: $csrf" >/dev/null

# A verified Destination without an encrypted secret must fail before provider
# allocation. This proves plaintext credentials are not optional in strict mode.
missing_session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
missing_status="$(curl -sS -o "$tmp_dir/missing-secret.json" -w '%{http_code}' --max-time 10 \
  -b "$cookie_jar" -X POST "$base_url/v1/sessions/$missing_session_id/prepare" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  -H "Idempotency-Key: missing-$missing_session_id" \
  --data "{\"environment\":\"dev\",\"destination_id\":\"$destination_id\"}")"
if [[ "$missing_status" != "409" ]]; then
  echo "prepare without Destination secret expected HTTP 409, got $missing_status" >&2
  exit 1
fi
missing_get="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 -b "$cookie_jar" "$base_url/v1/sessions/$missing_session_id")"
if [[ "$missing_get" != "404" ]]; then
  echo "failed prepare unexpectedly allocated Session state (HTTP $missing_get)" >&2
  exit 1
fi

secret_payload="$(python3 -c 'import json,sys; print(json.dumps({"value":sys.argv[1]}))' "$stream_key")"
secret_response="$(curl -fsS --max-time 10 -b "$cookie_jar" -X PUT \
  "$base_url/v1/destinations/$destination_id/secret" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  --data "$secret_payload")"
if grep -Fq "$stream_key" <<<"$secret_response"; then
  echo "Destination secret API echoed the plaintext secret" >&2
  exit 1
fi
python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("configured") is True, d' <<<"$secret_response"

secret_state="$("${compose[@]}" exec -T control-ui cat /state/destination_secrets.json)"
if grep -Fq "$stream_key" <<<"$secret_state"; then
  echo "plaintext Destination secret was persisted" >&2
  exit 1
fi
secret_mode="$("${compose[@]}" exec -T control-ui stat -c '%a' /state/destination_secrets.json)"
if [[ "$secret_mode" != "600" ]]; then
  echo "destination_secrets.json expected mode 600, got $secret_mode" >&2
  exit 1
fi

session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
prepared="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/prepare" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  -H "Idempotency-Key: egress-secret-$session_id" \
  --data "{\"environment\":\"dev\",\"destination_id\":\"$destination_id\"}")"
provider_server_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["provider_server_id"])' <<<"$prepared")"
python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d.get("destination_id") == sys.argv[1], d
' "$destination_id" <<<"$prepared"
if grep -Fq "$stream_key" <<<"$prepared"; then
  echo "Session prepare response leaked Destination secret" >&2
  exit 1
fi

export ASSIGNED_PROVIDER_SERVER_ID="$provider_server_id"
"${compose[@]}" up -d --build node-agent
wait_assigned_node "$session_id" 45
wait_secret_file 30

expected_url="$(python3 -c 'from urllib.parse import quote; import sys; print("rtmp://mediamtx:1935/output/relay/" + quote(sys.argv[1], safe=""))' "$stream_key")"
actual_url="$("${compose[@]}" exec -T node-agent cat /tmp/irlight-node-secrets/egress_url | tr -d '\r\n')"
if [[ "$actual_url" != "$expected_url" ]]; then
  echo "resolved egress URL mismatch" >&2
  exit 1
fi
node_secret_mode="$("${compose[@]}" exec -T node-agent stat -c '%a' /tmp/irlight-node-secrets/egress_url)"
if [[ "$node_secret_mode" != "600" ]]; then
  echo "Node egress_url expected mode 600, got $node_secret_mode" >&2
  exit 1
fi

state_dump="$("${compose[@]}" exec -T control-ui sh -c 'cat /state/catalog.json /state/sessions.json /state/nodes.json /state/destination_secrets.json')"
if grep -Fq "$stream_key" <<<"$state_dump"; then
  echo "plaintext Destination secret leaked into Control Plane state" >&2
  exit 1
fi
if grep -Fq "$expected_url" <<<"$state_dump"; then
  echo "credentialed egress URL leaked into Control Plane state" >&2
  exit 1
fi

node_listing="$(curl -fsS --max-time 5 "$base_url/internal/nodes")"
if grep -Fq "$stream_key" <<<"$node_listing" || grep -Fq "$expected_url" <<<"$node_listing"; then
  echo "Node API leaked credentialed egress URL" >&2
  exit 1
fi

echo "IRLight egress secret delivery smoke passed."
