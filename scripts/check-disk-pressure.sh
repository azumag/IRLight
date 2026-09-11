#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

path="${1:-${IRLIGHT_DISK_PATH:-${STATE_DIR:-/state}}}"
warning_percent="${IRLIGHT_DISK_WARNING_PERCENT:-80}"
critical_percent="${IRLIGHT_DISK_CRITICAL_PERCENT:-90}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_DISK_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

is_uint() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

if ! is_uint "$warning_percent" || ! is_uint "$critical_percent"; then
  unknown invalid_threshold
fi
if (( warning_percent < 0 || warning_percent > 100 || critical_percent < 0 || critical_percent > 100 )); then
  unknown invalid_threshold
fi
if (( warning_percent >= critical_percent )); then
  unknown invalid_threshold
fi

if [[ ! -e "$path" ]]; then
  unknown path_unavailable
fi

if ! df_output="$(df -P "$path" 2>/dev/null)"; then
  unknown df_failed
fi

metrics="$(printf '%s\n' "$df_output" | awk 'NR == 2 { print $4, $5 }')"
if ! read -r available_kb usage_field <<<"$metrics"; then
  unknown invalid_df_output
fi
usage_percent="${usage_field%%%}"

if ! is_uint "$available_kb" || ! is_uint "$usage_percent" || (( usage_percent > 100 )); then
  unknown invalid_df_output
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

printf 'IRLIGHT_DISK_PRESSURE status=%s usage_percent=%s available_kb=%s warning_percent=%s critical_percent=%s\n' \
  "$status" "$usage_percent" "$available_kb" "$warning_percent" "$critical_percent"
exit "$exit_code"
