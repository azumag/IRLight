#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-${IRLIGHT_CGROUP_MEMORY_CURRENT_PATH:-/sys/fs/cgroup/memory.current}}"
maximum_path="${2:-${IRLIGHT_CGROUP_MEMORY_MAX_PATH:-/sys/fs/cgroup/memory.max}}"
warning_percent="${IRLIGHT_CGROUP_MEMORY_WARNING_PERCENT:-80}"
critical_percent="${IRLIGHT_CGROUP_MEMORY_CRITICAL_PERCENT:-90}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_CGROUP_MEMORY_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
common_lib="$script_dir/lib/scalar-pressure-common.sh"
if [[ ! -r "$common_lib" ]]; then
  unknown shared_helper_unavailable
fi
# shellcheck source=lib/scalar-pressure-common.sh
source "$common_lib" || unknown shared_helper_unavailable

read_scalar() {
  local path="$1"
  local unavailable_reason="$2"
  local invalid_reason="$3"
  local -a lines

  if [[ ! -r "$path" ]]; then
    unknown "$unavailable_reason"
  fi
  mapfile -t lines < "$path" || unknown "$unavailable_reason"
  if (( ${#lines[@]} != 1 )); then
    unknown "$invalid_reason"
  fi

  local value extra
  if ! read -r value extra <<<"${lines[0]}"; then
    unknown "$invalid_reason"
  fi
  if [[ -z "${value:-}" || -n "${extra:-}" ]]; then
    unknown "$invalid_reason"
  fi
  REPLY="$value"
}

pressure_normalize_threshold_pair "$warning_percent" "$critical_percent" || unknown invalid_threshold
warning_percent="$PRESSURE_WARNING_PERCENT"
critical_percent="$PRESSURE_CRITICAL_PERCENT"

read_scalar "$current_path" current_unavailable invalid_current
current_raw="$REPLY"
pressure_normalize_int64_uint "$current_raw" || unknown invalid_current
current="$PRESSURE_VALUE"
current=$((10#$current))

read_scalar "$maximum_path" maximum_unavailable invalid_maximum
maximum_raw="$REPLY"

if [[ "$maximum_raw" == "max" ]]; then
  printf 'IRLIGHT_CGROUP_MEMORY_PRESSURE status=OK usage_percent=NA current_bytes=%s maximum_bytes=max warning_percent=%s critical_percent=%s\n' \
    "$current" \
    "$warning_percent" \
    "$critical_percent"
  exit 0
fi

pressure_normalize_int64_uint "$maximum_raw" || unknown invalid_maximum
maximum="$PRESSURE_VALUE"
maximum=$((10#$maximum))

# memory.max=0 is a valid finite configuration with no allocatable headroom.
if (( maximum == 0 )); then
  printf 'IRLIGHT_CGROUP_MEMORY_PRESSURE status=CRITICAL usage_percent=NO_HEADROOM current_bytes=%s maximum_bytes=0 warning_percent=%s critical_percent=%s\n' \
    "$current" \
    "$warning_percent" \
    "$critical_percent"
  exit 2
fi

# memory.current can temporarily remain above a newly lowered memory.max while
# reclaim/OOM handling catches up. Treat that as a real critical condition.
if (( current > maximum )); then
  printf 'IRLIGHT_CGROUP_MEMORY_PRESSURE status=CRITICAL usage_percent=OVER_LIMIT current_bytes=%s maximum_bytes=%s warning_percent=%s critical_percent=%s\n' \
    "$current" \
    "$maximum" \
    "$warning_percent" \
    "$critical_percent"
  exit 2
fi

pressure_usage_percent "$current" "$maximum" || unknown invalid_memory_values
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

printf 'IRLIGHT_CGROUP_MEMORY_PRESSURE status=%s usage_percent=%s current_bytes=%s maximum_bytes=%s warning_percent=%s critical_percent=%s\n' \
  "$status" \
  "$usage_percent" \
  "$current" \
  "$maximum" \
  "$warning_percent" \
  "$critical_percent"
exit "$exit_code"
