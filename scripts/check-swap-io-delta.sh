#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-${IRLIGHT_VMSTAT_PATH:-/proc/vmstat}}"
baseline_path="${2:-${IRLIGHT_VMSTAT_BASELINE_PATH:-}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_SWAP_IO status=UNKNOWN reason=%s\n' "$reason"
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

read_swap_io_counters() {
  local path="$1"
  local unavailable_reason="$2"
  local pswpin_name="$3"
  local pswpout_name="$4"
  local key=""
  local raw=""
  local extra=""
  local counter_value=""
  local pswpin_value=""
  local pswpout_value=""
  local seen_pswpin=0
  local seen_pswpout=0

  [[ -f "$path" && -r "$path" ]] || unknown "$unavailable_reason"
  while read -r key raw extra; do
    case "$key" in
      pswpin)
        ((seen_pswpin += 1))
        (( seen_pswpin == 1 )) || unknown invalid_vmstat_record
        [[ -z "$extra" ]] || unknown invalid_vmstat_record
        normalize_counter "$raw" counter_value || unknown invalid_vmstat_record
        pswpin_value="$counter_value"
        ;;
      pswpout)
        ((seen_pswpout += 1))
        (( seen_pswpout == 1 )) || unknown invalid_vmstat_record
        [[ -z "$extra" ]] || unknown invalid_vmstat_record
        normalize_counter "$raw" counter_value || unknown invalid_vmstat_record
        pswpout_value="$counter_value"
        ;;
    esac
  done < "$path" || unknown "$unavailable_reason"

  (( seen_pswpin == 1 && seen_pswpout == 1 )) || unknown swap_io_counters_unavailable
  printf -v "$pswpin_name" '%s' "$pswpin_value"
  printf -v "$pswpout_name" '%s' "$pswpout_value"
}

[[ -n "$baseline_path" ]] || unknown baseline_vmstat_unavailable

current_pswpin=""
current_pswpout=""
baseline_pswpin=""
baseline_pswpout=""
read_swap_io_counters "$current_path" current_vmstat_unavailable current_pswpin current_pswpout
read_swap_io_counters "$baseline_path" baseline_vmstat_unavailable baseline_pswpin baseline_pswpout

if (( 10#$current_pswpin < 10#$baseline_pswpin || 10#$current_pswpout < 10#$baseline_pswpout )); then
  unknown counter_reset
fi

pswpin_delta=$((10#$current_pswpin - 10#$baseline_pswpin))
pswpout_delta=$((10#$current_pswpout - 10#$baseline_pswpout))
if (( pswpin_delta > 0 || pswpout_delta > 0 )); then
  printf 'IRLIGHT_SWAP_IO status=WARNING reason=swap_io_activity pswpin_delta=%s pswpout_delta=%s\n' \
    "$pswpin_delta" "$pswpout_delta"
  exit 1
fi

printf 'IRLIGHT_SWAP_IO status=OK reason=none pswpin_delta=0 pswpout_delta=0\n'
exit 0
