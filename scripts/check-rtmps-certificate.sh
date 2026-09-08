#!/usr/bin/env bash
set -euo pipefail

cert_file="${1:-${NODE_RTMPS_CERT_FILE:-}}"
min_valid_seconds="${2:-${IRLIGHT_RTMPS_CERT_MIN_VALID_SECONDS:-0}}"

emit() {
  local status="$1"
  local code="$2"
  local not_after="${3:-}"
  if [[ -n "$not_after" ]]; then
    printf '{"status":"%s","code":"%s","min_valid_seconds":%s,"not_after":"%s"}\n' \
      "$status" "$code" "$min_valid_seconds" "$not_after"
  else
    printf '{"status":"%s","code":"%s","min_valid_seconds":%s}\n' \
      "$status" "$code" "$min_valid_seconds"
  fi
}

if [[ -z "$cert_file" ]]; then
  emit "ERROR" "RTMPS_CERT_PATH_REQUIRED"
  exit 2
fi

if [[ ! "$min_valid_seconds" =~ ^[0-9]+$ ]]; then
  min_valid_seconds=0
  emit "ERROR" "RTMPS_CERT_THRESHOLD_INVALID"
  exit 2
fi

if [[ ! -r "$cert_file" || ! -f "$cert_file" ]]; then
  emit "ERROR" "RTMPS_CERT_UNAVAILABLE"
  exit 2
fi

if ! command -v openssl >/dev/null 2>&1; then
  emit "ERROR" "OPENSSL_UNAVAILABLE"
  exit 2
fi

if ! enddate_output="$(openssl x509 -in "$cert_file" -noout -enddate 2>/dev/null)"; then
  emit "ERROR" "RTMPS_CERT_INVALID"
  exit 2
fi

not_after="${enddate_output#notAfter=}"
if [[ -z "$not_after" || "$not_after" == "$enddate_output" || "$not_after" == *'"'* || "$not_after" == *'\\'* ]]; then
  emit "ERROR" "RTMPS_CERT_INVALID"
  exit 2
fi

if openssl x509 -in "$cert_file" -noout -checkend "$min_valid_seconds" >/dev/null 2>&1; then
  emit "OK" "RTMPS_CERT_VALID" "$not_after"
  exit 0
fi

emit "WARNING" "RTMPS_CERT_EXPIRING" "$not_after"
exit 1
