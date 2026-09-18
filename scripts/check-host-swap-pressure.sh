#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

meminfo_path="${1:-${IRLIGHT_SWAP_MEMINFO_PATH:-/proc/meminfo}}"
warning_percent="${IRLIGHT_SWAP_WARNING_PERCENT:-80}"
critical_percent="${IRLIGHT_SWAP_CRITICAL_PERCENT:-90}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_HOST_SWAP_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
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

if [[ ! -r "$meminfo_path" ]]; then
  unknown meminfo_unavailable
fi

if ! metrics="$(
  awk '
    $1 == "SwapTotal:" {
      total_count += 1
      if ($3 != "kB") unit_invalid = 1
      total = $2
    }
    $1 == "SwapFree:" {
      free_count += 1
      if ($3 != "kB") unit_invalid = 1
      free = $2
    }
    END {
      if (total_count != 1 || free_count != 1 || unit_invalid) {
        exit 1
      }
      printf "%s %s\n", total, free
    }
  ' "$meminfo_path" 2>/dev/null
)"; then
  unknown invalid_meminfo
fi

if ! read -r total_raw free_raw extra <<<"$metrics" || [[ -n "${extra:-}" ]]; then
  unknown invalid_meminfo
fi

pressure_normalize_int64_uint "$total_raw" || unknown invalid_meminfo
total_kb="$PRESSURE_VALUE"
pressure_normalize_int64_uint "$free_raw" || unknown invalid_meminfo
free_kb="$PRESSURE_VALUE"

total_kb=$((10#$total_kb))
free_kb=$((10#$free_kb))
if (( free_kb > total_kb )); then
  unknown invalid_meminfo
fi

# A host with no configured swap is a valid operating policy. This check only
# evaluates pressure when the kernel reports a finite, non-zero swap pool.
if (( total_kb == 0 )); then
  printf 'IRLIGHT_HOST_SWAP_PRESSURE status=OK usage_percent=NA free_kb=0 total_kb=0 warning_percent=%s critical_percent=%s\n' \
    "$warning_percent" \
    "$critical_percent"
  exit 0
fi

used_kb=$((total_kb - free_kb))
pressure_usage_percent "$used_kb" "$total_kb" || unknown invalid_meminfo
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

printf 'IRLIGHT_HOST_SWAP_PRESSURE status=%s usage_percent=%s free_kb=%s total_kb=%s warning_percent=%s critical_percent=%s\n' \
  "$status" \
  "$usage_percent" \
  "$free_kb" \
  "$total_kb" \
  "$warning_percent" \
  "$critical_percent"
exit "$exit_code"
