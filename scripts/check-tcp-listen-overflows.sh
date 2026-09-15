#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-${IRLIGHT_TCP_NETSTAT_PATH:-/proc/net/netstat}}"
baseline_path="${2:-${IRLIGHT_TCP_NETSTAT_BASELINE_PATH:-}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_TCP_LISTEN_OVERFLOWS status=UNKNOWN reason=%s\n' "$reason"
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

read_tcp_ext_counters() {
  local path="$1"
  local unavailable_reason="$2"
  local prefix="$3"
  local -a tcp_ext_lines=()
  local -a headers=()
  local -a values=()
  local i key value
  local seen_listen_overflows=0
  local seen_listen_drops=0

  [[ -f "$path" && -r "$path" ]] || unknown "$unavailable_reason"
  mapfile -t tcp_ext_lines < <(grep '^TcpExt:' "$path" || true)
  if (( ${#tcp_ext_lines[@]} != 2 )); then
    unknown invalid_netstat_record
  fi

  read -r -a headers <<< "${tcp_ext_lines[0]}"
  read -r -a values <<< "${tcp_ext_lines[1]}"
  if (( ${#headers[@]} != ${#values[@]} || ${#headers[@]} < 2 )); then
    unknown invalid_netstat_record
  fi
  [[ "${headers[0]}" == "TcpExt:" && "${values[0]}" == "TcpExt:" ]] || unknown invalid_netstat_record

  for ((i = 1; i < ${#headers[@]}; i++)); do
    key="${headers[$i]}"
    value="${values[$i]}"

    case "$key" in
      ListenOverflows)
        (( seen_listen_overflows == 0 )) || unknown invalid_netstat_record
        is_uint64_bounded "$value" || unknown invalid_netstat_record
        seen_listen_overflows=1
        printf -v "${prefix}_listen_overflows" '%d' "$((10#$value))"
        ;;
      ListenDrops)
        (( seen_listen_drops == 0 )) || unknown invalid_netstat_record
        is_uint64_bounded "$value" || unknown invalid_netstat_record
        seen_listen_drops=1
        printf -v "${prefix}_listen_drops" '%d' "$((10#$value))"
        ;;
    esac
  done

  if (( seen_listen_overflows == 0 || seen_listen_drops == 0 )); then
    unknown invalid_netstat_record
  fi
}

[[ -n "$baseline_path" ]] || unknown baseline_netstat_unavailable

read_tcp_ext_counters "$current_path" current_netstat_unavailable current
read_tcp_ext_counters "$baseline_path" baseline_netstat_unavailable baseline

for counter in listen_overflows listen_drops; do
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
if (( delta_listen_overflows > 0 || delta_listen_drops > 0 )); then
  status="WARNING"
  reason="tcp_listen_queue_pressure"
  exit_code=1
fi

printf 'IRLIGHT_TCP_LISTEN_OVERFLOWS status=%s reason=%s listen_overflows_delta=%s listen_drops_delta=%s\n' \
  "$status" \
  "$reason" \
  "$delta_listen_overflows" \
  "$delta_listen_drops"
exit "$exit_code"
