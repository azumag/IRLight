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
  local prefix="$3"
  local -a lines=()
  local -a fields=()
  local line dropped squeeze
  local total_dropped=0
  local total_squeeze=0
  local records=0

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

    dropped=$((16#${fields[1]}))
    squeeze=$((16#${fields[2]}))
    total_dropped=$((total_dropped + dropped))
    total_squeeze=$((total_squeeze + squeeze))
    records=$((records + 1))
  done

  (( records > 0 )) || unknown invalid_softnet_record
  printf -v "${prefix}_dropped" '%d' "$total_dropped"
  printf -v "${prefix}_time_squeeze" '%d' "$total_squeeze"
}

[[ -n "$baseline_path" ]] || unknown baseline_softnet_unavailable

read_softnet_counters "$current_path" current_softnet_unavailable current
read_softnet_counters "$baseline_path" baseline_softnet_unavailable baseline

for counter in dropped time_squeeze; do
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
