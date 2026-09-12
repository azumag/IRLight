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

for threshold in "$warning_percent" "$critical_percent"; do
  if ! is_uint "$threshold" || (( ${#threshold} > 3 )); then
    unknown invalid_threshold
  fi
done

# Force decimal so values such as 080 are not interpreted as octal by Bash.
warning_percent=$((10#$warning_percent))
critical_percent=$((10#$critical_percent))
if (( warning_percent > 100 || critical_percent > 100 || warning_percent >= critical_percent )); then
  unknown invalid_threshold
fi

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
normalize_int64_uint "$running_raw" || unknown invalid_loadavg
running="$REPLY"
normalize_int64_uint "$total_raw" || unknown invalid_loadavg
total="$REPLY"
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
normalize_int64_uint "$maximum_raw" || unknown invalid_threads_max
maximum="$REPLY"
maximum=$((10#$maximum))
if (( maximum <= 0 || total > maximum )); then
  unknown invalid_task_values
fi

usage_percent="$(
  awk -v total="$total" -v maximum="$maximum" \
    'BEGIN { printf "%d", (total * 100.0) / maximum }'
)"
if ! is_uint "$usage_percent" || (( ${#usage_percent} > 3 )); then
  unknown invalid_task_values
fi
usage_percent=$((10#$usage_percent))
if (( usage_percent > 100 )); then
  unknown invalid_task_values
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

printf 'IRLIGHT_TASK_PRESSURE status=%s usage_percent=%s running=%s total=%s maximum=%s warning_percent=%s critical_percent=%s\n' \
  "$status" \
  "$usage_percent" \
  "$running" \
  "$total" \
  "$maximum" \
  "$warning_percent" \
  "$critical_percent"
exit "$exit_code"
