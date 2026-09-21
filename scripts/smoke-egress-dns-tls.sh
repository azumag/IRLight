#!/usr/bin/env bash
set -euo pipefail
umask 077
export EGRESS_RTMP_SINK_FACTORY="${EGRESS_RTMP_SINK_FACTORY:-rtmpsink}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
smoke_project="irlight-egress-dns-tls-smoke-$$-$RANDOM"
tmp_dir="$(mktemp -d)"
override="$tmp_dir/egress-dns-tls.override.yml"
dns_secret="$tmp_dir/dns-egress-url"
tls_secret="$tmp_dir/tls-egress-url"
stream_key_file="$tmp_dir/tls-stream-key"
stream_key="ci-egress-tls-secret-$RANDOM"

cat >"$dns_secret" <<'EOF'
rtmp://egress-dns-does-not-exist.invalid/live/dns-test
EOF
chmod 600 "$dns_secret"

printf '%s' "$stream_key" >"$stream_key_file"
cat >"$tls_secret" <<EOF
rtmps://egress-tls-target:1936/live/$stream_key
EOF
chmod 600 "$tls_secret" "$stream_key_file"
unset stream_key

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
      EGRESS_RTMP_SINK_FACTORY: "${EGRESS_RTMP_SINK_FACTORY}"
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
      EGRESS_RTMP_SINK_FACTORY: "${EGRESS_RTMP_SINK_FACTORY}"
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

compose=(docker compose -p "$smoke_project" -f "$repo_root/docker-compose.poc.yml" -f "$override")

redact_generated_secrets() {
  python3 -c '
from pathlib import Path
import sys

secret = Path(sys.argv[1]).read_bytes()
if not secret:
    raise SystemExit(1)
data = sys.stdin.buffer.read()
sys.stdout.buffer.write(data.replace(secret, b"<redacted>"))
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

status_matches_reason() {
  local expected_status="$1"
  local expected_reason="$2"
  python3 -c '
import json,sys
try:
    value=json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if value.get("status") == sys.argv[1] and value.get("reason_code") == sys.argv[2] else 1)
' "$expected_status" "$expected_reason"
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
  if (( logs_rc != 0 )); then
    echo "failed to read $service diagnostics: rc=$logs_rc" >&2
    return "$logs_rc"
  fi
}

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps -a >&2 || true
    echo "--- DNS egress logs (redacted) ---" >&2
    emit_redacted_compose_logs egress-dns 120 || true
    echo "--- TLS egress logs (redacted) ---" >&2
    emit_redacted_compose_logs egress-tls 160 || true
    echo "--- TLS target logs (redacted) ---" >&2
    emit_redacted_compose_logs egress-tls-target 120 || true
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
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
    if status_matches_reason "$expected_status" "$expected_reason" <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  local safe_payload
  if safe_payload="$(printf '%s' "$payload" | redact_generated_secrets 2>/dev/null)"; then
    echo "egress status did not become $expected_status/$expected_reason; last=$safe_payload" >&2
  else
    echo "egress status did not become $expected_status/$expected_reason; last=<redaction-failed>" >&2
  fi
  return 1
}

# The generated project must never borrow or tear down a developer stack. If a
# fixed host port is occupied, let `up` fail rather than preempting that owner.
"${compose[@]}" config >/dev/null
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
if ! stdin_excludes_stream_key <<<"$status_payload"; then
  echo "egress status stream-key check failed closed" >&2
  exit 1
fi

egress_tls_logs="$tmp_dir/egress-tls.log"
if ! "${compose[@]}" logs --no-color egress-tls >"$egress_tls_logs" 2>&1; then
  echo "failed to read egress TLS logs for secret redaction check" >&2
  exit 1
fi
if ! file_excludes_stream_key "$egress_tls_logs"; then
  echo "egress TLS log stream-key check failed closed" >&2
  exit 1
fi

if ! "${compose[@]}" ps --status running --services | grep -qx continuity; then
  echo "continuity stopped during DNS/TLS destination failures" >&2
  exit 1
fi

echo "IRLight egress DNS/TLS failure smoke passed."
