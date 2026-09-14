#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-${IRLIGHT_UDP_SNMP_PATH:-/proc/net/snmp}}"
baseline_path="${2:-${IRLIGHT_UDP_SNMP_BASELINE_PATH:-}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_UDP_SNMP_ERRORS status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

is_uint64_bounded() {
  local value="$1"
  [[ "$value" =~ ^[0-9]+$ ]] || return 1
  (( ${#value} <= 19 )) || return 1
  if (( ${#value} == 19 )) && [[ "$value" > "9223372036854775807" ]]; then
    return 1
  fi
  return 0
}

read_udp_counters() {
  local path="$1"
  local unavailable_reason="$2"
  local prefix="$3"
  local -a udp_lines=()
  local -a headers=()
  local -a values=()
  local i key value
  local seen_in_errors=0
  local seen_rcvbuf_errors=0
  local seen_sndbuf_errors=0
  local seen_csum_errors=0

  [[ -f "$path" && -r "$path" ]] || unknown "$unavailable_reason"
  mapfile -t udp_lines < <(grep '^Udp:' "$path" || true)
  if (( ${#udp_lines[@]} != 2 )); then
    unknown invalid_snmp_record
  fi

  read -r -a headers <<< "${udp_lines[0]}"
  read -r -a values <<< "${udp_lines[1]}"
  if (( ${#headers[@]} != ${#values[@]} || ${#headers[@]} < 2 )); then
    unknown invalid_snmp_record
  fi
  [[ "${headers[0]}" == "Udp:" && "${values[0]}" == "Udp:" ]] || unknown invalid_snmp_record

  for ((i = 1; i < ${#headers[@]}; i++)); do
    key="${headers[$i]}"
    value="${values[$i]}"
    is_uint64_bounded "$value" || unknown invalid_snmp_record

    case "$key" in
      InErrors)
        (( seen_in_errors == 0 )) || unknown invalid_snmp_record
        seen_in_errors=1
        printf -v "${prefix}_in_errors" '%d' "$((10#$value))"
        ;;
      RcvbufErrors)
        (( seen_rcvbuf_errors == 0 )) || unknown invalid_snmp_record
        seen_rcvbuf_errors=1
        printf -v "${prefix}_rcvbuf_errors" '%d' "$((10#$value))"
        ;;
      SndbufErrors)
        (( seen_sndbuf_errors == 0 )) || unknown invalid_snmp_record
        seen_sndbuf_errors=1
        printf -v "${prefix}_sndbuf_errors" '%d' "$((10#$value))"
        ;;
      InCsumErrors)
        (( seen_csum_errors == 0 )) || unknown invalid_snmp_record
        seen_csum_errors=1
        printf -v "${prefix}_csum_errors" '%d' "$((10#$value))"
        ;;
    esac
  done

  if (( seen_in_errors == 0 || seen_rcvbuf_errors == 0 || seen_sndbuf_errors == 0 || seen_csum_errors == 0 )); then
    unknown invalid_snmp_record
  fi
}

[[ -n "$baseline_path" ]] || unknown baseline_snmp_unavailable

read_udp_counters "$current_path" current_snmp_unavailable current
read_udp_counters "$baseline_path" baseline_snmp_unavailable baseline

for counter in in_errors rcvbuf_errors sndbuf_errors csum_errors; do
  current_var="current_${counter}"
  baseline_var="baseline_${counter}"
  current_value="${!current_var}"
  baseline_value="${!baseline_var}"
  if (( current_value < baseline_value )); then
    unknown counter_reset
  fi
  printf -v "delta_${counter}" '%d' "$((current_value - baseline_value))"
done

status="OK"
reason="none"
exit_code=0
if (( delta_in_errors > 0 || delta_rcvbuf_errors > 0 || delta_sndbuf_errors > 0 || delta_csum_errors > 0 )); then
  status="WARNING"
  reason="udp_error_activity"
  exit_code=1
fi

printf 'IRLIGHT_UDP_SNMP_ERRORS status=%s reason=%s in_errors_delta=%s rcvbuf_errors_delta=%s sndbuf_errors_delta=%s csum_errors_delta=%s\n' \
  "$status" \
  "$reason" \
  "$delta_in_errors" \
  "$delta_rcvbuf_errors" \
  "$delta_sndbuf_errors" \
  "$delta_csum_errors"
exit "$exit_code"
