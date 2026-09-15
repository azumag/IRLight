#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-${IRLIGHT_TCP_SNMP_PATH:-/proc/net/snmp}}"
baseline_path="${2:-${IRLIGHT_TCP_SNMP_BASELINE_PATH:-}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_TCP_SNMP_ATTEMPT_FAILS status=UNKNOWN reason=%s\n' "$reason"
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

read_tcp_counter() {
  local path="$1"
  local unavailable_reason="$2"
  local prefix="$3"
  local -a tcp_lines=()
  local -a headers=()
  local -a values=()
  local i key value
  local seen_attempt_fails=0

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

    if [[ "$key" == "AttemptFails" ]]; then
      (( seen_attempt_fails == 0 )) || unknown invalid_snmp_record
      is_uint64_bounded "$value" || unknown invalid_snmp_record
      seen_attempt_fails=1
      printf -v "${prefix}_attempt_fails" '%d' "$((10#$value))"
    fi
  done

  (( seen_attempt_fails == 1 )) || unknown invalid_snmp_record
}

[[ -n "$baseline_path" ]] || unknown baseline_snmp_unavailable

read_tcp_counter "$current_path" current_snmp_unavailable current
read_tcp_counter "$baseline_path" baseline_snmp_unavailable baseline

if (( current_attempt_fails < baseline_attempt_fails )); then
  unknown counter_reset
fi

delta_attempt_fails="$((current_attempt_fails - baseline_attempt_fails))"

status="OK"
reason="none"
exit_code=0
if (( delta_attempt_fails > 0 )); then
  status="WARNING"
  reason="tcp_connection_attempt_failures"
  exit_code=1
fi

printf 'IRLIGHT_TCP_SNMP_ATTEMPT_FAILS status=%s reason=%s attempt_fails_delta=%s\n' \
  "$status" \
  "$reason" \
  "$delta_attempt_fails"
exit "$exit_code"
