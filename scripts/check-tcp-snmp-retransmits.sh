#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-${IRLIGHT_TCP_SNMP_PATH:-/proc/net/snmp}}"
baseline_path="${2:-${IRLIGHT_TCP_SNMP_BASELINE_PATH:-}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_TCP_SNMP_RETRANSMITS status=UNKNOWN reason=%s\n' "$reason"
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

read_tcp_counters() {
  local path="$1"
  local unavailable_reason="$2"
  local prefix="$3"
  local -a tcp_lines=()
  local -a headers=()
  local -a values=()
  local i key value
  local seen_out_segments=0
  local seen_retrans_segments=0

  [[ -f "$path" && -r "$path" ]] || unknown "$unavailable_reason"
  mapfile -t tcp_lines < <(grep '^Tcp:' "$path" || true)
  if (( ${#tcp_lines[@]} != 2 )); then
    unknown invalid_snmp_record
  fi

  read -r -a headers <<< "${tcp_lines[0]}"
  read -r -a values <<< "${tcp_lines[1]}"
  if (( ${#headers[@]} != ${#values[@]} || ${#headers[@]} < 2 )); then
    unknown invalid_snmp_record
  fi
  [[ "${headers[0]}" == "Tcp:" && "${values[0]}" == "Tcp:" ]] || unknown invalid_snmp_record

  for ((i = 1; i < ${#headers[@]}; i++)); do
    key="${headers[$i]}"
    value="${values[$i]}"

    case "$key" in
      OutSegs)
        (( seen_out_segments == 0 )) || unknown invalid_snmp_record
        is_uint64_bounded "$value" || unknown invalid_snmp_record
        seen_out_segments=1
        printf -v "${prefix}_out_segments" '%d' "$((10#$value))"
        ;;
      RetransSegs)
        (( seen_retrans_segments == 0 )) || unknown invalid_snmp_record
        is_uint64_bounded "$value" || unknown invalid_snmp_record
        seen_retrans_segments=1
        printf -v "${prefix}_retrans_segments" '%d' "$((10#$value))"
        ;;
    esac
  done

  if (( seen_out_segments == 0 || seen_retrans_segments == 0 )); then
    unknown invalid_snmp_record
  fi
}

[[ -n "$baseline_path" ]] || unknown baseline_snmp_unavailable

read_tcp_counters "$current_path" current_snmp_unavailable current
read_tcp_counters "$baseline_path" baseline_snmp_unavailable baseline

for counter in out_segments retrans_segments; do
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
if (( delta_retrans_segments > 0 )); then
  status="WARNING"
  reason="tcp_retransmit_activity"
  exit_code=1
fi

printf 'IRLIGHT_TCP_SNMP_RETRANSMITS status=%s reason=%s out_segments_delta=%s retrans_segments_delta=%s\n' \
  "$status" \
  "$reason" \
  "$delta_out_segments" \
  "$delta_retrans_segments"
exit "$exit_code"
