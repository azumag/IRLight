#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp_dir="$(mktemp -d)"
override="$tmp_dir/egress-dns-tls.override.yml"
dns_secret="$tmp_dir/dns-egress-url"
tls_secret="$tmp_dir/tls-egress-url"
stream_key="ci-egress-tls-secret-$RANDOM"

cat >"$dns_secret" <<'EOF'
rtmp://egress-dns-does-not-exist.invalid/live/dns-test
EOF
chmod 600 "$dns_secret"

cat >"$tls_secret" <<EOF
rtmps://egress-tls-target:1936/live/$stream_key
EOF
chmod 600 "$tls_secret"

openssl req -x509 -newkey rsa:2048 -sha256 -nodes \
  -keyout "$tmp_dir/server.key" \
  -out "$tmp_dir/server.crt" \
  -days 1 \
  -subj '/CN=egress-tls-target' \
  -addext 'subjectAltName=DNS:egress-tls-target' >/dev/null 2>&1
chmod 600 "$tmp_dir/server.key"

cat >"$override" <<EOF
services:
  egress-tls-target:
    image: bluenviron/mediamtx:1.20.0
    restart: "no"
    environment:
      MTX_RTMPENCRYPTION: optional
      MTX_RTMPSADDRESS: :1936
      MTX_RTMPSERVERKEY: /run/irlight/tls/server.key
      MTX_RTMPSERVERCERT: /run/irlight/tls/server.crt
    volumes:
      - $tmp_dir/server.key:/run/irlight/tls/server.key:ro
      - $tmp_dir/server.crt:/run/irlight/tls/server.crt:ro

  egress-dns:
    build:
      context: ./apps/egress-gateway
      dockerfile: Dockerfile
    restart: "no"
    depends_on:
      - mediamtx
      - continuity
    environment:
      EGRESS_INPUT_URI_FILE: /run/irlight/relay-secrets/media_relay_uri
      EGRESS_URL_FILE: /run/irlight/secrets/egress_url
      EGRESS_STATUS_FILE: /state/egress.json
      EGRESS_ALLOW_PRIVATE_TARGETS: "0"
      EGRESS_CONNECT_TIMEOUT_SECONDS: "8"
      EGRESS_RETRY_INITIAL_SECONDS: "30"
      EGRESS_RETRY_MAX_SECONDS: "30"
      EGRESS_RETRY_MULTIPLIER: "2"
      EGRESS_RETRY_JITTER_RATIO: "0"
      EGRESS_MAX_ATTEMPTS: "0"
      EGRESS_MAX_RETRY_SECONDS: "0"
    volumes:
      - irlight-state:/state
      - irlight-relay-secrets:/run/irlight/relay-secrets:ro
      - $dns_secret:/run/irlight/secrets/egress_url:ro

  egress-tls:
    build:
      context: ./apps/egress-gateway
      dockerfile: Dockerfile
    restart: "no"
    depends_on:
      - mediamtx
      - continuity
      - egress-tls-target
    environment:
      EGRESS_INPUT_URI_FILE: /run/irlight/relay-secrets/media_relay_uri
      EGRESS_URL_FILE: /run/irlight/secrets/egress_url
      EGRESS_STATUS_FILE: /state/egress.json
      # The TLS target is intentionally on the isolated Compose network. The
      # test is about certificate validation, not private-address policy.
      EGRESS_ALLOW_PRIVATE_TARGETS: "1"
      EGRESS_CONNECT_TIMEOUT_SECONDS: "10"
      EGRESS_RETRY_INITIAL_SECONDS: "30"
      EGRESS_RETRY_MAX_SECONDS: "30"
      EGRESS_RETRY_MULTIPLIER: "2"
      EGRESS_RETRY_JITTER_RATIO: "0"
      EGRESS_MAX_ATTEMPTS: "0"
      EGRESS_MAX_RETRY_SECONDS: "0"
    volumes:
      - irlight-state:/state
      - irlight-relay-secrets:/run/irlight/relay-secrets:ro
      - $tls_secret:/run/irlight/secrets/egress_url:ro
EOF

compose=(docker compose -f "$repo_root/docker-compose.poc.yml" -f "$override")

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps -a >&2 || true
    echo "--- DNS egress logs ---" >&2
    "${compose[@]}" logs --no-color --tail=120 egress-dns >&2 || true
    echo "--- TLS egress logs ---" >&2
    "${compose[@]}" logs --no-color --tail=160 egress-tls >&2 || true
    echo "--- TLS target logs ---" >&2
    "${compose[@]}" logs --no-color --tail=120 egress-tls-target >&2 || true
  fi
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT

read_status() {
  "${compose[@]}" exec -T continuity cat /state/egress.json 2>/dev/null || true
}

wait_status_reason() {
  local expected_status="$1"
  local expected_reason="$2"
  local timeout="${3:-45}"
  local deadline=$((SECONDS + timeout))
  local payload=""
  while (( SECONDS < deadline )); do
    payload="$(read_status)"
    if python3 -c '
import json,sys
try:
    value=json.loads(sys.argv[1])
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if value.get("status") == sys.argv[2] and value.get("reason_code") == sys.argv[3] else 1)
' "$payload" "$expected_status" "$expected_reason" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "egress status did not become $expected_status/$expected_reason; last=$payload" >&2
  return 1
}

"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
# Continuity consumes Agent-generated authenticated local-media URIs, so start
# the complete PoC dependency chain before introducing the two test gateways.
"${compose[@]}" up -d --build mediamtx continuity control-ui node-agent egress-tls-target

# Runtime DNS lookup fails before GStreamer is created. It is retryable and
# therefore must surface as RECONNECTING/DNS_FAILED rather than an opaque error.
"${compose[@]}" up -d egress-dns
wait_status_reason RECONNECTING DNS_FAILED 45
"${compose[@]}" stop -t 5 egress-dns >/dev/null

# The target certificate is deliberately self-signed and is not mounted into
# the Egress Gateway trust store. RTMPS must not silently disable certificate
# validation; the failed handshake must be classified as TLS_FAILED.
"${compose[@]}" up -d egress-tls
wait_status_reason RECONNECTING TLS_FAILED 60

status_payload="$(read_status)"
if grep -Fq "$stream_key" <<<"$status_payload"; then
  echo "egress status leaked TLS stream key" >&2
  exit 1
fi
if "${compose[@]}" logs --no-color egress-tls | grep -Fq "$stream_key"; then
  echo "egress TLS logs leaked stream key" >&2
  exit 1
fi

if ! "${compose[@]}" ps --status running --services | grep -qx continuity; then
  echo "continuity stopped during DNS/TLS destination failures" >&2
  exit 1
fi

echo "IRLight egress DNS/TLS failure smoke passed."
