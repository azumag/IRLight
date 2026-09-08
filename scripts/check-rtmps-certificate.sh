#!/usr/bin/env bash
set -euo pipefail

cert_file="${1:-${NODE_RTMPS_CERT_FILE:-}}"
min_valid_seconds="${2:-${IRLIGHT_RTMPS_CERT_MIN_VALID_SECONDS:-0}}"

emit() {
  local status="$1"
  local code="$2"
  local not_before="${3:-}"
  local not_after="${4:-}"
  printf '{"status":"%s","code":"%s","min_valid_seconds":%s' \
    "$status" "$code" "$min_valid_seconds"
  if [[ -n "$not_before" ]]; then
    printf ',"not_before":"%s"' "$not_before"
  fi
  if [[ -n "$not_after" ]]; then
    printf ',"not_after":"%s"' "$not_after"
  fi
  printf '}\n'
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

if ! command -v date >/dev/null 2>&1; then
  emit "ERROR" "DATE_UNAVAILABLE"
  exit 2
fi

if ! startdate_output="$(openssl x509 -in "$cert_file" -noout -startdate 2>/dev/null)"; then
  emit "ERROR" "RTMPS_CERT_INVALID"
  exit 2
fi
if ! enddate_output="$(openssl x509 -in "$cert_file" -noout -enddate 2>/dev/null)"; then
  emit "ERROR" "RTMPS_CERT_INVALID"
  exit 2
fi

not_before="${startdate_output#notBefore=}"
not_after="${enddate_output#notAfter=}"
for value in "$not_before" "$not_after"; do
  if [[ -z "$value" || "$value" == *'"'* || "$value" == *'\\'* ]]; then
    emit "ERROR" "RTMPS_CERT_INVALID"
    exit 2
  fi
done
if [[ "$not_before" == "$startdate_output" || "$not_after" == "$enddate_output" ]]; then
  emit "ERROR" "RTMPS_CERT_INVALID"
  exit 2
fi

if ! not_before_epoch="$(LC_ALL=C date -u -d "$not_before" +%s 2>/dev/null)"; then
  emit "ERROR" "RTMPS_CERT_TIME_UNREADABLE"
  exit 2
fi
if ! now_epoch="$(date -u +%s 2>/dev/null)"; then
  emit "ERROR" "RTMPS_CERT_TIME_UNREADABLE"
  exit 2
fi
if [[ ! "$not_before_epoch" =~ ^[0-9]+$ || ! "$now_epoch" =~ ^[0-9]+$ ]]; then
  emit "ERROR" "RTMPS_CERT_TIME_UNREADABLE"
  exit 2
fi

if (( not_before_epoch > now_epoch )); then
  emit "ERROR" "RTMPS_CERT_NOT_YET_VALID" "$not_before" "$not_after"
  exit 2
fi

if openssl x509 -in "$cert_file" -noout -checkend "$min_valid_seconds" >/dev/null 2>&1; then
  emit "OK" "RTMPS_CERT_VALID" "$not_before" "$not_after"
  exit 0
fi

emit "WARNING" "RTMPS_CERT_EXPIRING" "$not_before" "$not_after"
exit 1
