#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

path="${1:-${IRLIGHT_DISK_PATH:-${STATE_DIR:-/state}}}"
warning_percent="${IRLIGHT_DISK_WARNING_PERCENT:-80}"
critical_percent="${IRLIGHT_DISK_CRITICAL_PERCENT:-90}"
inode_warning_percent="${IRLIGHT_DISK_INODE_WARNING_PERCENT:-$warning_percent}"
inode_critical_percent="${IRLIGHT_DISK_INODE_CRITICAL_PERCENT:-$critical_percent}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_DISK_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

is_uint() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

for threshold in \
  "$warning_percent" \
  "$critical_percent" \
  "$inode_warning_percent" \
  "$inode_critical_percent"
do
  if ! is_uint "$threshold" || (( ${#threshold} > 3 )); then
    unknown invalid_threshold
  fi
done

# Force decimal to avoid Bash treating values such as 080 as octal.
warning_percent=$((10#$warning_percent))
critical_percent=$((10#$critical_percent))
inode_warning_percent=$((10#$inode_warning_percent))
inode_critical_percent=$((10#$inode_critical_percent))

if ((
  warning_percent > 100 ||
  critical_percent > 100 ||
  warning_percent >= critical_percent ||
  inode_warning_percent > 100 ||
  inode_critical_percent > 100 ||
  inode_warning_percent >= inode_critical_percent
)); then
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

if ! is_uint "$available_kb" || ! is_uint "$usage_percent" || (( ${#usage_percent} > 3 )); then
  unknown invalid_df_output
fi
usage_percent=$((10#$usage_percent))
if (( usage_percent > 100 )); then
  unknown invalid_df_output
fi

if ! inode_df_output="$(df -Pi "$path" 2>/dev/null)"; then
  unknown df_inode_failed
fi

inode_metrics="$(printf '%s\n' "$inode_df_output" | awk 'NR == 2 { print $4, $5 }')"
if ! read -r available_inodes inode_usage_field <<<"$inode_metrics"; then
  unknown invalid_inode_df_output
fi
inode_usage_percent="${inode_usage_field%%%}"

if ! is_uint "$available_inodes" || ! is_uint "$inode_usage_percent" || (( ${#inode_usage_percent} > 3 )); then
  unknown invalid_inode_df_output
fi
inode_usage_percent=$((10#$inode_usage_percent))
if (( inode_usage_percent > 100 )); then
  unknown invalid_inode_df_output
fi

status="OK"
exit_code=0
if (( usage_percent >= critical_percent || inode_usage_percent >= inode_critical_percent )); then
  status="CRITICAL"
  exit_code=2
elif (( usage_percent >= warning_percent || inode_usage_percent >= inode_warning_percent )); then
  status="WARNING"
  exit_code=1
fi

printf 'IRLIGHT_DISK_PRESSURE status=%s usage_percent=%s available_kb=%s inode_usage_percent=%s available_inodes=%s warning_percent=%s critical_percent=%s inode_warning_percent=%s inode_critical_percent=%s\n' \
  "$status" \
  "$usage_percent" \
  "$available_kb" \
  "$inode_usage_percent" \
  "$available_inodes" \
  "$warning_percent" \
  "$critical_percent" \
  "$inode_warning_percent" \
  "$inode_critical_percent"
exit "$exit_code"
