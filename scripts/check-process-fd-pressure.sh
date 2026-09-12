#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

fd_dir="${1:-${IRLIGHT_PROCESS_FD_DIR:-}}"
limits_path="${2:-${IRLIGHT_PROCESS_LIMITS_PATH:-}}"
warning_percent="${IRLIGHT_PROCESS_FD_WARNING_PERCENT:-80}"
critical_percent="${IRLIGHT_PROCESS_FD_CRITICAL_PERCENT:-90}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_PROCESS_FD_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
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

if [[ -z "$fd_dir" || -z "$limits_path" ]]; then
  unknown target_required
fi

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

if [[ ! -d "$fd_dir" || ! -r "$fd_dir" ]]; then
  unknown fd_directory_unavailable
fi
if [[ ! -r "$limits_path" ]]; then
  unknown limits_unavailable
fi

soft_limit=""
hard_limit=""
limit_count=0
while IFS= read -r line; do
  if [[ "$line" =~ ^Max[[:space:]]+open[[:space:]]+files[[:space:]]+([^[:space:]]+)[[:space:]]+([^[:space:]]+)[[:space:]]+files[[:space:]]*$ ]]; then
    limit_count=$((limit_count + 1))
    soft_limit="${BASH_REMATCH[1]}"
    hard_limit="${BASH_REMATCH[2]}"
  elif [[ "$line" == Max*open*files* ]]; then
    unknown invalid_limits
  fi
done < "$limits_path" || unknown limits_unavailable

if (( limit_count != 1 )); then
  unknown invalid_limits
fi

if [[ "$soft_limit" != "unlimited" ]]; then
  normalize_int64_uint "$soft_limit" || unknown invalid_limits
  soft_limit="$REPLY"
fi
if [[ "$hard_limit" != "unlimited" ]]; then
  normalize_int64_uint "$hard_limit" || unknown invalid_limits
  hard_limit="$REPLY"
fi
if [[ "$soft_limit" == "unlimited" && "$hard_limit" != "unlimited" ]]; then
  unknown invalid_limits
fi
if [[ "$soft_limit" != "unlimited" && "$hard_limit" != "unlimited" ]]; then
  soft_limit=$((10#$soft_limit))
  hard_limit=$((10#$hard_limit))
  if (( soft_limit > hard_limit )); then
    unknown invalid_limits
  fi
fi

shopt -s nullglob dotglob
entries=("$fd_dir"/*)
fd_count=${#entries[@]}
for entry in "${entries[@]}"; do
  name="${entry##*/}"
  if ! is_uint "$name"; then
    unknown invalid_fd_entry
  fi
done
# A target process may disappear while its descriptors are being enumerated.
# Fail closed instead of reporting zero headroom usage for a vanished target.
if [[ ! -d "$fd_dir" || ! -r "$limits_path" ]]; then
  unknown target_disappeared
fi

if [[ "$soft_limit" == "unlimited" ]]; then
  printf 'IRLIGHT_PROCESS_FD_PRESSURE status=OK usage_percent=NA open_fds=%s soft_limit=unlimited hard_limit=%s warning_percent=%s critical_percent=%s\n' \
    "$fd_count" "$hard_limit" "$warning_percent" "$critical_percent"
  exit 0
fi

if (( soft_limit == 0 )); then
  printf 'IRLIGHT_PROCESS_FD_PRESSURE status=CRITICAL usage_percent=NO_HEADROOM open_fds=%s soft_limit=0 hard_limit=%s warning_percent=%s critical_percent=%s\n' \
    "$fd_count" "$hard_limit" "$warning_percent" "$critical_percent"
  exit 2
fi

if (( fd_count > soft_limit )); then
  printf 'IRLIGHT_PROCESS_FD_PRESSURE status=CRITICAL usage_percent=OVER_LIMIT open_fds=%s soft_limit=%s hard_limit=%s warning_percent=%s critical_percent=%s\n' \
    "$fd_count" "$soft_limit" "$hard_limit" "$warning_percent" "$critical_percent"
  exit 2
fi

usage_percent="$(awk -v current="$fd_count" -v maximum="$soft_limit" 'BEGIN { printf "%d", (current * 100.0) / maximum }')"
if ! is_uint "$usage_percent" || (( ${#usage_percent} > 3 )); then
  unknown invalid_fd_values
fi
usage_percent=$((10#$usage_percent))
if (( usage_percent > 100 )); then
  unknown invalid_fd_values
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

printf 'IRLIGHT_PROCESS_FD_PRESSURE status=%s usage_percent=%s open_fds=%s soft_limit=%s hard_limit=%s warning_percent=%s critical_percent=%s\n' \
  "$status" "$usage_percent" "$fd_count" "$soft_limit" "$hard_limit" "$warning_percent" "$critical_percent"
exit "$exit_code"
