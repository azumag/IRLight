#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

loadavg_path="${1:-${IRLIGHT_LOADAVG_PATH:-/proc/loadavg}}"
cpu_count="${2:-${IRLIGHT_CPU_COUNT:-}}"
warning_percent="${IRLIGHT_LOAD_WARNING_PERCENT:-100}"
critical_percent="${IRLIGHT_LOAD_CRITICAL_PERCENT:-200}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_LOAD_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

is_uint() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

for threshold in "$warning_percent" "$critical_percent"; do
  if ! is_uint "$threshold" || (( ${#threshold} > 4 )); then
    unknown invalid_threshold
  fi
done

# Force decimal so values such as 0100 are never treated as octal by Bash.
warning_percent=$((10#$warning_percent))
critical_percent=$((10#$critical_percent))
if (( warning_percent <= 0 || critical_percent <= 0 || warning_percent >= critical_percent )); then
  unknown invalid_threshold
fi

if [[ -z "$cpu_count" ]]; then
  if ! cpu_count="$(getconf _NPROCESSORS_ONLN 2>/dev/null)"; then
    unknown cpu_count_unavailable
  fi
fi
if ! is_uint "$cpu_count" || (( ${#cpu_count} > 6 )); then
  unknown invalid_cpu_count
fi
cpu_count=$((10#$cpu_count))
if (( cpu_count <= 0 )); then
  unknown invalid_cpu_count
fi

if [[ ! -r "$loadavg_path" ]]; then
  unknown loadavg_unavailable
fi

load1=""
load5=""
load15=""
if ! read -r load1 load5 load15 _ < "$loadavg_path"; then
  unknown invalid_loadavg
fi

# Linux /proc/loadavg reports decimal fixed-point values. Reject exponent notation,
# signs, NaN/Infinity and unreasonably large/corrupt fields before numeric use.
for value in "$load1" "$load5" "$load15"; do
  if [[ ! "$value" =~ ^[0-9]{1,9}([.][0-9]{1,6})?$ ]]; then
    unknown invalid_loadavg
  fi
done

if ! load_percent="$(
  awk -v load="$load5" -v cpus="$cpu_count" 'BEGIN {
    if (cpus <= 0) exit 1
    value = int((load * 100) / cpus)
    if (value < 0) exit 1
    printf "%d", value
  }'
)"; then
  unknown invalid_loadavg
fi
if ! is_uint "$load_percent" || (( ${#load_percent} > 9 )); then
  unknown invalid_loadavg
fi
load_percent=$((10#$load_percent))

status="OK"
exit_code=0
if (( load_percent >= critical_percent )); then
  status="CRITICAL"
  exit_code=2
elif (( load_percent >= warning_percent )); then
  status="WARNING"
  exit_code=1
fi

printf 'IRLIGHT_LOAD_PRESSURE status=%s load5=%s cpu_count=%s load_percent=%s warning_percent=%s critical_percent=%s\n' \
  "$status" \
  "$load5" \
  "$cpu_count" \
  "$load_percent" \
  "$warning_percent" \
  "$critical_percent"
exit "$exit_code"
