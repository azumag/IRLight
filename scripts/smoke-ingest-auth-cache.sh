#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -f docker-compose.poc.yml)
base_url="${BASE_URL:-http://127.0.0.1:8080}"
cookie_jar="/tmp/irlight-cache-smoke-cookies.txt"
publisher_pid=""
email="cache-smoke-$(date +%s)-$RANDOM@example.invalid"
password="SmokePassword123!"
csrf=""
session_id=""
ingest_username=""
ingest_secret=""
credential_id=""

cleanup() {
  status=$?
  if [[ -n "$publisher_pid" ]]; then
    kill "$publisher_pid" 2>/dev/null || true
    wait "$publisher_pid" 2>/dev/null || true
  fi
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps >&2 || true
    echo "--- node-agent logs ---" >&2
    "${compose[@]}" logs --no-color --tail=150 node-agent >&2 || true
    echo "--- mediamtx logs ---" >&2
    "${compose[@]}" logs --no-color --tail=150 mediamtx >&2 || true
    echo "--- control logs ---" >&2
    "${compose[@]}" logs --no-color --tail=100 control-ui >&2 || true
  fi
  rm -f "$cookie_jar"
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

wait_http() {
  local deadline=$((SECONDS + ${2:-45}))
  until curl -fsS --max-time 3 "$1" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "HTTP endpoint did not become ready: $1" >&2
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

wait_node_registered() {
  local deadline=$((SECONDS + 45))
  while (( SECONDS < deadline )); do
    payload="$(curl -fsS --max-time 3 "$base_url/internal/nodes" 2>/dev/null || true)"
    if python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("nodes") else 1)' <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "node-agent did not register" >&2
  return 1
}

start_publisher() {
  local duration="${1:-7}"
  "${compose[@]}" exec -T continuity sh -c "
    timeout --signal=INT ${duration}s gst-launch-1.0 -q -e \
      flvmux name=mux streamable=true ! rtmp2sink location='rtmp://mediamtx:1935/live/input?user=${ingest_username}&pass=${ingest_secret}' \
      videotestsrc is-live=true pattern=smpte ! \
        video/x-raw,width=1280,height=720,framerate=30/1,format=I420 ! \
        x264enc tune=zerolatency speed-preset=veryfast bitrate=1200 key-int-max=60 bframes=0 ! \
        video/x-h264,profile=main ! h264parse config-interval=-1 ! queue ! mux. \
      audiotestsrc is-live=true wave=sine freq=440 ! audioconvert ! audioresample ! \
        audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=128000 ! aacparse ! queue ! mux.
  " >/tmp/irlight-cache-publisher.log 2>&1 &
  publisher_pid=$!
}

path_state() {
  local expected="$1"
  "${compose[@]}" exec -T node-agent python3 -c '
import json, sys, urllib.request
expected = sys.argv[1] == "online"
with urllib.request.urlopen("http://mediamtx:9997/v3/paths/list?itemsPerPage=100", timeout=2) as r:
    data = json.load(r)
item = next((x for x in data.get("items", []) if x.get("name") == "live/input"), None)
online = bool(item and item.get("online"))
raise SystemExit(0 if online == expected else 1)
' "$expected"
}

wait_path_state() {
  local expected="$1"
  local deadline=$((SECONDS + ${2:-20}))
  while (( SECONDS < deadline )); do
    if path_state "$expected" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "live/input did not become $expected" >&2
  return 1
}

proxy_auth_status() {
  local expected_secret="${1:-$ingest_secret}"
  "${compose[@]}" exec -T \
    -e AUTH_USER="$ingest_username" \
    -e AUTH_SECRET="$expected_secret" \
    node-agent python3 -c '
import json, os, urllib.error, urllib.request
payload = {
    "user": os.environ["AUTH_USER"],
    "password": os.environ["AUTH_SECRET"],
    "token": "",
    "ip": "198.51.100.77",
    "action": "publish",
    "path": "live/input",
    "protocol": "rtmp",
    "id": "cache-smoke-direct",
    "query": "",
    "userAgent": "cache-smoke",
}
req = urllib.request.Request(
    "http://127.0.0.1:8090/auth",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=3) as r:
        print(r.status)
except urllib.error.HTTPError as exc:
    print(exc.code)
'
}

"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" up -d --build
wait_http "$base_url/healthz" 60
wait_node_registered

curl -fsS --max-time 10 -X POST "$base_url/v1/auth/register" \
  -H 'Content-Type: application/json' \
  --data "{\"email\":\"$email\",\"password\":\"$password\",\"display_name\":\"Cache Smoke\"}" >/dev/null
login

session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/prepare" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  -H "Idempotency-Key: cache-smoke-$session_id" \
  --data '{"environment":"dev"}' >/dev/null

credential="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/ingest-credentials" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  --data '{"protocols":["rtmp"],"ttl_seconds":3600}')"
ingest_username="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])' <<<"$credential")"
ingest_secret="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["credential_secret"])' <<<"$credential")"
credential_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$credential")"

# Prime the node-local cache through the real MediaMTX external-auth call.
start_publisher 7
wait_path_state online 20
wait "$publisher_pid" || true
publisher_pid=""
wait_path_state offline 15

# Control Plane outage: the same credential must reconnect through the cached
# positive decision. A different secret must not receive fallback authorization.
"${compose[@]}" stop control-ui >/dev/null
start_publisher 7
wait_path_state online 20
wrong_status="$(proxy_auth_status wrong-secret)"
if [[ "$wrong_status" != "503" ]]; then
  echo "uncached wrong secret expected 503 during outage, got $wrong_status" >&2
  exit 1
fi
wait "$publisher_pid" || true
publisher_pid=""
wait_path_state offline 15

# Restore Control Plane, explicitly revoke the credential, and force a direct
# auth check. Explicit 401 must evict the positive cache immediately.
"${compose[@]}" start control-ui >/dev/null
wait_http "$base_url/healthz" 45
login
curl -fsS --max-time 10 -b "$cookie_jar" -X DELETE \
  "$base_url/v1/sessions/$session_id/ingest-credentials/$credential_id" \
  -H "X-CSRF-Token: $csrf" >/dev/null
revoked_status="$(proxy_auth_status)"
if [[ "$revoked_status" != "401" ]]; then
  echo "revoked credential expected explicit 401, got $revoked_status" >&2
  exit 1
fi

# Once the explicit denial evicted the cache, a second CP outage must fail
# closed instead of reviving the stale credential.
"${compose[@]}" stop control-ui >/dev/null
post_revoke_status="$(proxy_auth_status)"
if [[ "$post_revoke_status" != "503" ]]; then
  echo "revoked cache entry was reused during outage (HTTP $post_revoke_status)" >&2
  exit 1
fi

echo "IRLight node-local ingest auth cache smoke passed."
