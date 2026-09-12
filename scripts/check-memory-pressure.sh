#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

meminfo_path="${1:-${IRLIGHT_MEMINFO_PATH:-/proc/meminfo}}"
warning_percent="${IRLIGHT_MEMORY_WARNING_PERCENT:-80}"
critical_percent="${IRLIGHT_MEMORY_CRITICAL_PERCENT:-90}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_MEMORY_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

is_uint() {
  [[ "$1" =~ ^[0-9]+$ ]]
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

if [[ ! -r "$meminfo_path" ]]; then
  unknown meminfo_unavailable
fi

if ! metrics="$(
  awk '
    $1 == "MemTotal:" {
      total_count += 1
      if ($3 != "kB") unit_invalid = 1
      total = $2
    }
    $1 == "MemAvailable:" {
      available_count += 1
      if ($3 != "kB") unit_invalid = 1
      available = $2
    }
    END {
      if (total_count != 1 || available_count != 1 || unit_invalid) {
        exit 1
      }
      printf "%s %s\n", total, available
    }
  ' "$meminfo_path" 2>/dev/null
)"; then
  unknown invalid_meminfo
fi

if ! read -r total_kb available_kb <<<"$metrics"; then
  unknown invalid_meminfo
fi

if ! is_uint "$total_kb" || ! is_uint "$available_kb"; then
  unknown invalid_meminfo
fi

# Keep integer comparisons inside signed 64-bit arithmetic. Real Linux memory
# values are many orders of magnitude smaller; oversized input is corruption.
if (( ${#total_kb} > 18 || ${#available_kb} > 18 )); then
  unknown invalid_meminfo
fi

total_kb=$((10#$total_kb))
available_kb=$((10#$available_kb))
if (( total_kb <= 0 || available_kb > total_kb )); then
  unknown invalid_meminfo
fi

used_kb=$((total_kb - available_kb))
usage_percent="$(
  awk -v used="$used_kb" -v total="$total_kb" 'BEGIN { printf "%d", (used * 100) / total }'
)"
if ! is_uint "$usage_percent" || (( ${#usage_percent} > 3 )); then
  unknown invalid_meminfo
fi
usage_percent=$((10#$usage_percent))
if (( usage_percent > 100 )); then
  unknown invalid_meminfo
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

printf 'IRLIGHT_MEMORY_PRESSURE status=%s usage_percent=%s available_kb=%s total_kb=%s warning_percent=%s critical_percent=%s\n' \
  "$status" \
  "$usage_percent" \
  "$available_kb" \
  "$total_kb" \
  "$warning_percent" \
  "$critical_percent"
exit "$exit_code"
