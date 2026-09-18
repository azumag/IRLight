#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-${IRLIGHT_PROC_STAT_PATH:-/proc/stat}}"
baseline_path="${2:-${IRLIGHT_PROC_STAT_BASELINE_PATH:-}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_CPU_STEAL status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

normalize_counter() {
  local raw="$1"
  local normalized="$raw"

  [[ "$raw" =~ ^[0-9]+$ ]] || return 1
  while [[ ${#normalized} -gt 1 && "${normalized:0:1}" == "0" ]]; do
    normalized="${normalized:1}"
  done
  if (( ${#normalized} > 19 )); then
    return 1
  fi
  if (( ${#normalized} == 19 )) && [[ "$normalized" > "9223372036854775807" ]]; then
    return 1
  fi
  printf '%s\n' "$normalized"
}

read_cpu_counters() {
  local path="$1"
  local unavailable_reason="$2"
  local output_name="$3"
  local -n output_ref="$output_name"
  local -a lines=()
  local -a fields=()
  local -a counters=()
  local line
  local normalized=""
  local found=0

  [[ -f "$path" && -r "$path" ]] || unknown "$unavailable_reason"
  if ! mapfile -t lines < "$path"; then
    unknown "$unavailable_reason"
  fi

  for line in "${lines[@]}"; do
    read -r -a fields <<< "$line"
    [[ "${fields[0]:-}" == "cpu" ]] || continue
    ((found += 1))
    (( found == 1 )) || unknown invalid_cpu_stat_record
    (( ${#fields[@]} >= 9 )) || unknown invalid_cpu_stat_record

    counters=()
    for index in {1..8}; do
      if ! normalized="$(normalize_counter "${fields[$index]}")"; then
        unknown invalid_cpu_stat_record
      fi
      counters+=("$normalized")
    done
  done

  (( found == 1 )) || unknown cpu_aggregate_unavailable
  output_ref=("${counters[@]}")
}

[[ -n "$baseline_path" ]] || unknown baseline_proc_stat_unavailable

current=()
baseline=()
read_cpu_counters "$current_path" current_proc_stat_unavailable current
read_cpu_counters "$baseline_path" baseline_proc_stat_unavailable baseline

for index in "${!current[@]}"; do
  if (( 10#${current[$index]} < 10#${baseline[$index]} )); then
    unknown counter_reset
  fi
done

steal_delta=$((10#${current[7]} - 10#${baseline[7]}))
if (( steal_delta > 0 )); then
  printf 'IRLIGHT_CPU_STEAL status=WARNING reason=cpu_steal_activity steal_ticks_delta=%s\n' \
    "$steal_delta"
  exit 1
fi

printf 'IRLIGHT_CPU_STEAL status=OK reason=none steal_ticks_delta=0\n'
exit 0
