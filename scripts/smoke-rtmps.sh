#!/usr/bin/env bash
set -euo pipefail

base_url="${BASE_URL:-http://127.0.0.1:8080}"
tmp_dir="$(mktemp -d)"
override="$tmp_dir/rtmps.override.yml"
cookie_jar="$tmp_dir/cookies.txt"

cleanup() {
  status=$?
  docker compose -f docker-compose.poc.yml -f "$override" down --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT

openssl req -x509 -newkey rsa:2048 -sha256 -nodes \
  -keyout "$tmp_dir/server.key" \
  -out "$tmp_dir/server.crt" \
  -days 1 \
  -subj '/CN=mediamtx' \
  -addext 'subjectAltName=DNS:mediamtx' >/dev/null 2>&1
chmod 600 "$tmp_dir/server.key"

cat >"$override" <<EOF
services:
  mediamtx:
    environment:
      MTX_RTMPENCRYPTION: optional
      MTX_RTMPSADDRESS: :1936
      MTX_RTMPSERVERKEY: /run/irlight/tls/server.key
      MTX_RTMPSERVERCERT: /run/irlight/tls/server.crt
    volumes:
      - $tmp_dir/server.key:/run/irlight/tls/server.key:ro
      - $tmp_dir/server.crt:/run/irlight/tls/server.crt:ro
  control-ui:
    environment:
      SSL_CERT_FILE: /run/irlight/tls/ca.crt
      IRLIGHT_VERIFY_ALLOW_PRIVATE_TARGETS: "1"
    volumes:
      - $tmp_dir/server.crt:/run/irlight/tls/ca.crt:ro
EOF

compose=(docker compose -f docker-compose.poc.yml -f "$override")
"${compose[@]}" down --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" config >/dev/null
"${compose[@]}" up -d --build

for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 "$base_url/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS --max-time 3 "$base_url/healthz" >/dev/null

email="rtmps-smoke-$(date +%s)-$RANDOM@example.invalid"
password='SmokePassword123!'
curl -fsS --max-time 10 -X POST "$base_url/v1/auth/register" \
  -H 'Content-Type: application/json' \
  --data "{\"email\":\"$email\",\"password\":\"$password\",\"display_name\":\"RTMPS Smoke\"}" >/dev/null

login="$(curl -fsS --max-time 10 -c "$cookie_jar" -X POST "$base_url/v1/auth/login" \
  -H 'Content-Type: application/json' \
  --data "{\"email\":\"$email\",\"password\":\"$password\"}")"
csrf="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])' <<<"$login")"

destination="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST "$base_url/v1/destinations" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  --data '{"type":"rtmps","display_name":"Local RTMPS probe","server_url":"rtmps://mediamtx:1936/live/input","secret_ref":"smoke/rtmps"}')"
destination_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$destination")"
verified="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/destinations/$destination_id/verify" \
  -H "X-CSRF-Token: $csrf")"

python3 - "$verified" <<'PY'
import json
import sys

item = json.loads(sys.argv[1])
assert item["verification_status"] == "VERIFIED", item
transport = item["verification_transport"]
assert transport["protocol"] == "rtmps", transport
assert transport["peer_port"] == 1936, transport
PY

echo "IRLight RTMPS TLS smoke test passed."
