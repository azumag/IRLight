#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-${IRLIGHT_SOFTNET_STAT_PATH:-/proc/net/softnet_stat}}"
baseline_path="${2:-${IRLIGHT_SOFTNET_STAT_BASELINE_PATH:-}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_SOFTNET_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

is_softnet_hex() {
  [[ "$1" =~ ^[0-9A-Fa-f]{8}$ ]]
}

read_softnet_counters() {
  local path="$1"
  local unavailable_reason="$2"
  local processed_name="$3"
  local dropped_name="$4"
  local squeeze_name="$5"
  local -n processed_ref="$processed_name"
  local -n dropped_ref="$dropped_name"
  local -n squeeze_ref="$squeeze_name"
  local -a lines=()
  local -a fields=()
  local line

  [[ -f "$path" && -r "$path" ]] || unknown "$unavailable_reason"
  if ! mapfile -t lines < "$path"; then
    unknown "$unavailable_reason"
  fi
  (( ${#lines[@]} > 0 )) || unknown invalid_softnet_record

  for line in "${lines[@]}"; do
    [[ -n "$line" ]] || unknown invalid_softnet_record
    read -r -a fields <<< "$line"
    (( ${#fields[@]} >= 3 )) || unknown invalid_softnet_record
    is_softnet_hex "${fields[0]}" || unknown invalid_softnet_record
    is_softnet_hex "${fields[1]}" || unknown invalid_softnet_record
    is_softnet_hex "${fields[2]}" || unknown invalid_softnet_record

    processed_ref+=("$((16#${fields[0]}))")
    dropped_ref+=("$((16#${fields[1]}))")
    squeeze_ref+=("$((16#${fields[2]}))")
  done
}

[[ -n "$baseline_path" ]] || unknown baseline_softnet_unavailable

current_processed=()
current_dropped=()
current_time_squeeze=()
baseline_processed=()
baseline_dropped=()
baseline_time_squeeze=()
read_softnet_counters \
  "$current_path" current_softnet_unavailable \
  current_processed current_dropped current_time_squeeze
read_softnet_counters \
  "$baseline_path" baseline_softnet_unavailable \
  baseline_processed baseline_dropped baseline_time_squeeze

if (( ${#current_processed[@]} != ${#baseline_processed[@]} )); then
  unknown cpu_topology_changed
fi

delta_dropped=0
delta_time_squeeze=0
for ((i = 0; i < ${#current_processed[@]}; i++)); do
  if ((
    current_processed[i] < baseline_processed[i]
    || current_dropped[i] < baseline_dropped[i]
    || current_time_squeeze[i] < baseline_time_squeeze[i]
  )); then
    unknown counter_reset
  fi
  delta_dropped=$((delta_dropped + current_dropped[i] - baseline_dropped[i]))
  delta_time_squeeze=$((
    delta_time_squeeze + current_time_squeeze[i] - baseline_time_squeeze[i]
  ))
done

status="OK"
reason="none"
exit_code=0
if (( delta_dropped > 0 || delta_time_squeeze > 0 )); then
  status="WARNING"
  reason="softnet_pressure_activity"
  exit_code=1
fi

printf 'IRLIGHT_SOFTNET_PRESSURE status=%s reason=%s dropped_delta=%s time_squeeze_delta=%s\n' \
  "$status" \
  "$reason" \
  "$delta_dropped" \
  "$delta_time_squeeze"
exit "$exit_code"
