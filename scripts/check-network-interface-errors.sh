#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_stats_dir="${1:-${IRLIGHT_NETWORK_STATS_DIR:-}}"
baseline_stats_dir="${2:-${IRLIGHT_NETWORK_STATS_BASELINE_DIR:-}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_NETWORK_INTERFACE_ERRORS status=UNKNOWN reason=%s\n' "$reason"
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

read_counter() {
  local path="$1"
  local unavailable_reason="$2"
  local -a lines=()
  local value

  [[ -f "$path" && -r "$path" ]] || unknown "$unavailable_reason"
  mapfile -t lines < "$path" || unknown "$unavailable_reason"
  if (( ${#lines[@]} != 1 )); then
    unknown invalid_stats_record
  fi

  value="${lines[0]}"
  if ! is_uint64_bounded "$value"; then
    unknown invalid_stats_record
  fi

  REPLY=$((10#$value))
}

[[ -n "$current_stats_dir" && -d "$current_stats_dir" && -r "$current_stats_dir" ]] || unknown current_stats_unavailable
[[ -n "$baseline_stats_dir" && -d "$baseline_stats_dir" && -r "$baseline_stats_dir" ]] || unknown baseline_stats_unavailable

declare -A current=()
declare -A baseline=()
declare -A delta=()

for key in rx_errors tx_errors rx_dropped tx_dropped; do
  read_counter "$current_stats_dir/$key" current_stats_unavailable
  current[$key]="$REPLY"
  read_counter "$baseline_stats_dir/$key" baseline_stats_unavailable
  baseline[$key]="$REPLY"

  if (( current[$key] < baseline[$key] )); then
    unknown counter_reset
  fi
  delta[$key]=$(( current[$key] - baseline[$key] ))
done

status="OK"
reason="none"
exit_code=0

if (( delta[rx_errors] > 0 || delta[tx_errors] > 0 )); then
  status="CRITICAL"
  reason="interface_error_activity"
  exit_code=2
elif (( delta[rx_dropped] > 0 || delta[tx_dropped] > 0 )); then
  status="WARNING"
  reason="interface_drop_activity"
  exit_code=1
fi

printf 'IRLIGHT_NETWORK_INTERFACE_ERRORS status=%s reason=%s rx_errors_delta=%s tx_errors_delta=%s rx_dropped_delta=%s tx_dropped_delta=%s\n' \
  "$status" \
  "$reason" \
  "${delta[rx_errors]}" \
  "${delta[tx_errors]}" \
  "${delta[rx_dropped]}" \
  "${delta[tx_dropped]}"
exit "$exit_code"
