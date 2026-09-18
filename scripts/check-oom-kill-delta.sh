#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-${IRLIGHT_VMSTAT_PATH:-/proc/vmstat}}"
baseline_path="${2:-${IRLIGHT_VMSTAT_BASELINE_PATH:-}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_OOM_KILL status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

normalize_counter() {
  local raw="$1"
  local output_name="$2"
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
  printf -v "$output_name" '%s' "$normalized"
}

read_oom_kill_counter() {
  local path="$1"
  local unavailable_reason="$2"
  local output_name="$3"
  local key=""
  local raw=""
  local extra=""
  local value=""
  local found=0

  [[ -f "$path" && -r "$path" ]] || unknown "$unavailable_reason"
  while read -r key raw extra; do
    [[ "$key" == "oom_kill" ]] || continue
    ((found += 1))
    (( found == 1 )) || unknown invalid_vmstat_record
    [[ -z "$extra" ]] || unknown invalid_vmstat_record
    normalize_counter "$raw" value || unknown invalid_vmstat_record
  done < "$path" || unknown "$unavailable_reason"

  (( found == 1 )) || unknown oom_kill_counter_unavailable
  printf -v "$output_name" '%s' "$value"
}

[[ -n "$baseline_path" ]] || unknown baseline_vmstat_unavailable

current_oom_kill=""
baseline_oom_kill=""
read_oom_kill_counter "$current_path" current_vmstat_unavailable current_oom_kill
read_oom_kill_counter "$baseline_path" baseline_vmstat_unavailable baseline_oom_kill

if (( 10#$current_oom_kill < 10#$baseline_oom_kill )); then
  unknown counter_reset
fi

delta=$((10#$current_oom_kill - 10#$baseline_oom_kill))
if (( delta > 0 )); then
  printf 'IRLIGHT_OOM_KILL status=CRITICAL reason=oom_kill_detected oom_kill_delta=%s\n' "$delta"
  exit 2
fi

printf 'IRLIGHT_OOM_KILL status=OK reason=none oom_kill_delta=0\n'
exit 0
