#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

loadavg_path="${1:-${IRLIGHT_LOADAVG_PATH:-/proc/loadavg}}"
threads_max_path="${2:-${IRLIGHT_THREADS_MAX_PATH:-/proc/sys/kernel/threads-max}}"
warning_percent="${IRLIGHT_TASK_WARNING_PERCENT:-80}"
critical_percent="${IRLIGHT_TASK_CRITICAL_PERCENT:-90}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_TASK_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
common_lib="$script_dir/lib/scalar-pressure-common.sh"
if [[ ! -r "$common_lib" ]]; then
  unknown shared_helper_unavailable
fi
# shellcheck source=lib/scalar-pressure-common.sh
source "$common_lib" || unknown shared_helper_unavailable

pressure_normalize_threshold_pair "$warning_percent" "$critical_percent" || unknown invalid_threshold
warning_percent="$PRESSURE_WARNING_PERCENT"
critical_percent="$PRESSURE_CRITICAL_PERCENT"

if [[ ! -r "$loadavg_path" ]]; then
  unknown loadavg_unavailable
fi
mapfile -t loadavg_lines < "$loadavg_path" || unknown loadavg_unavailable
if (( ${#loadavg_lines[@]} != 1 )); then
  unknown invalid_loadavg
fi
if ! read -r load1 load5 load15 tasks_field last_pid extra <<<"${loadavg_lines[0]}"; then
  unknown invalid_loadavg
fi
if [[ -n "${extra:-}" || -z "${load1:-}" || -z "${load5:-}" || -z "${load15:-}" || -z "${tasks_field:-}" || -z "${last_pid:-}" ]]; then
  unknown invalid_loadavg
fi
if [[ ! "$tasks_field" =~ ^([0-9]+)/([0-9]+)$ ]]; then
  unknown invalid_loadavg
fi
running_raw="${BASH_REMATCH[1]}"
total_raw="${BASH_REMATCH[2]}"
pressure_normalize_int64_uint "$running_raw" || unknown invalid_loadavg
running="$PRESSURE_VALUE"
pressure_normalize_int64_uint "$total_raw" || unknown invalid_loadavg
total="$PRESSURE_VALUE"
running=$((10#$running))
total=$((10#$total))
if (( total <= 0 || running > total )); then
  unknown invalid_loadavg
fi

if [[ ! -r "$threads_max_path" ]]; then
  unknown threads_max_unavailable
fi
mapfile -t threads_max_lines < "$threads_max_path" || unknown threads_max_unavailable
if (( ${#threads_max_lines[@]} != 1 )); then
  unknown invalid_threads_max
fi
if ! read -r maximum_raw extra <<<"${threads_max_lines[0]}"; then
  unknown invalid_threads_max
fi
if [[ -n "${extra:-}" || -z "${maximum_raw:-}" ]]; then
  unknown invalid_threads_max
fi
pressure_normalize_int64_uint "$maximum_raw" || unknown invalid_threads_max
maximum="$PRESSURE_VALUE"
maximum=$((10#$maximum))
if (( maximum <= 0 || total > maximum )); then
  unknown invalid_task_values
fi

pressure_usage_percent "$total" "$maximum" || unknown invalid_task_values
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

printf 'IRLIGHT_TASK_PRESSURE status=%s usage_percent=%s running=%s total=%s maximum=%s warning_percent=%s critical_percent=%s\n' \
  "$status" \
  "$usage_percent" \
  "$running" \
  "$total" \
  "$maximum" \
  "$warning_percent" \
  "$critical_percent"
exit "$exit_code"
