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

is_uint() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

normalize_int64_uint() {
  local value="$1"
  if ! is_uint "$value"; then
    return 1
  fi
  while [[ ${#value} -gt 1 && ${value:0:1} == "0" ]]; do
    value="${value:1}"
  done
  if (( ${#value} > 19 )); then
    return 1
  fi
  if (( ${#value} == 19 )) && [[ "$value" > "9223372036854775807" ]]; then
    return 1
  fi
  REPLY="$value"
}

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

for threshold in "$warning_percent" "$critical_percent"; do
  if ! is_uint "$threshold" || (( ${#threshold} > 3 )); then
    unknown invalid_threshold
  fi
done

warning_percent=$((10#$warning_percent))
critical_percent=$((10#$critical_percent))
if (( warning_percent > 100 || critical_percent > 100 || warning_percent >= critical_percent )); then
  unknown invalid_threshold
fi

read_scalar "$current_path" current_unavailable invalid_current
current_raw="$REPLY"
normalize_int64_uint "$current_raw" || unknown invalid_current
current="$REPLY"
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

normalize_int64_uint "$maximum_raw" || unknown invalid_maximum
maximum="$REPLY"
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

usage_percent="$(
  awk -v current="$current" -v maximum="$maximum" \
    'BEGIN { printf "%d", (current * 100.0) / maximum }'
)"
if ! is_uint "$usage_percent" || (( ${#usage_percent} > 3 )); then
  unknown invalid_memory_values
fi
usage_percent=$((10#$usage_percent))
if (( usage_percent > 100 )); then
  unknown invalid_memory_values
fi

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
