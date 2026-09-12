#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

count_path="${1:-${IRLIGHT_CONNTRACK_COUNT_PATH:-/proc/sys/net/netfilter/nf_conntrack_count}}"
max_path="${2:-${IRLIGHT_CONNTRACK_MAX_PATH:-/proc/sys/net/netfilter/nf_conntrack_max}}"
warning_percent="${IRLIGHT_CONNTRACK_WARNING_PERCENT:-80}"
critical_percent="${IRLIGHT_CONNTRACK_CRITICAL_PERCENT:-90}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_CONNTRACK_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
common_lib="$script_dir/lib/scalar-pressure-common.sh"
if [[ ! -r "$common_lib" ]]; then
  unknown shared_helper_unavailable
fi
# shellcheck source=lib/scalar-pressure-common.sh
source "$common_lib" || unknown shared_helper_unavailable

read_single_uint() {
  local path="$1"
  local unavailable_reason="$2"
  local invalid_reason="$3"
  local -a lines
  local raw

  if [[ ! -r "$path" ]]; then
    unknown "$unavailable_reason"
  fi
  mapfile -t lines < "$path" || unknown "$unavailable_reason"
  if (( ${#lines[@]} != 1 )); then
    unknown "$invalid_reason"
  fi
  raw="${lines[0]}"
  if [[ "$raw" =~ [[:space:]] ]]; then
    unknown "$invalid_reason"
  fi
  pressure_normalize_int64_uint "$raw" || unknown "$invalid_reason"
  REPLY="$PRESSURE_VALUE"
}

pressure_normalize_threshold_pair "$warning_percent" "$critical_percent" || unknown invalid_threshold
warning_percent="$PRESSURE_WARNING_PERCENT"
critical_percent="$PRESSURE_CRITICAL_PERCENT"

read_single_uint "$count_path" conntrack_count_unavailable invalid_conntrack_count
count="$REPLY"
read_single_uint "$max_path" conntrack_max_unavailable invalid_conntrack_max
maximum="$REPLY"

count=$((10#$count))
maximum=$((10#$maximum))
if (( maximum <= 0 || count > maximum )); then
  unknown invalid_conntrack_values
fi

pressure_usage_percent "$count" "$maximum" || unknown invalid_conntrack_values
usage_percent="$PRESSURE_VALUE"

status="OK"
exit_code=0
if (( usage_percent >= critical_percent )); then
  status="CRITICAL"
  exit_code=2
elif (( usage_percent >= warning_percent )); then
  status="WARNING"
  exit_code=1
fi

printf 'IRLIGHT_CONNTRACK_PRESSURE status=%s usage_percent=%s count=%s maximum=%s warning_percent=%s critical_percent=%s\n' \
  "$status" \
  "$usage_percent" \
  "$count" \
  "$maximum" \
  "$warning_percent" \
  "$critical_percent"
exit "$exit_code"
