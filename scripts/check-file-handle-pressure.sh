#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

file_nr_path="${1:-${IRLIGHT_FILE_NR_PATH:-/proc/sys/fs/file-nr}}"
warning_percent="${IRLIGHT_FILE_HANDLE_WARNING_PERCENT:-80}"
critical_percent="${IRLIGHT_FILE_HANDLE_CRITICAL_PERCENT:-90}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_FILE_HANDLE_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
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

if [[ ! -r "$file_nr_path" ]]; then
  unknown file_nr_unavailable
fi

mapfile -t lines < "$file_nr_path" || unknown file_nr_unavailable
if (( ${#lines[@]} != 1 )); then
  unknown invalid_file_nr
fi

if ! read -r allocated_raw unused_raw maximum_raw extra <<<"${lines[0]}"; then
  unknown invalid_file_nr
fi
if [[ -n "${extra:-}" || -z "${allocated_raw:-}" || -z "${unused_raw:-}" || -z "${maximum_raw:-}" ]]; then
  unknown invalid_file_nr
fi

normalize_int64_uint "$allocated_raw" || unknown invalid_file_nr
allocated="$REPLY"
normalize_int64_uint "$unused_raw" || unknown invalid_file_nr
unused="$REPLY"
normalize_int64_uint "$maximum_raw" || unknown invalid_file_nr
maximum="$REPLY"

allocated=$((10#$allocated))
unused=$((10#$unused))
maximum=$((10#$maximum))

if (( maximum <= 0 || unused > allocated || allocated > maximum )); then
  unknown invalid_file_nr
fi

# file-nr reports allocated handles, unused allocated handles, and the system
# maximum. On modern kernels unused is normally zero, but subtract it so older
# kernels are evaluated by active handles rather than allocated capacity.
active=$((allocated - unused))
usage_percent="$(
  awk -v active="$active" -v maximum="$maximum" \
    'BEGIN { printf "%d", (active * 100.0) / maximum }'
)"
if ! is_uint "$usage_percent" || (( ${#usage_percent} > 3 )); then
  unknown invalid_file_nr
fi
usage_percent=$((10#$usage_percent))
if (( usage_percent > 100 )); then
  unknown invalid_file_nr
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

printf 'IRLIGHT_FILE_HANDLE_PRESSURE status=%s usage_percent=%s active=%s allocated=%s unused=%s maximum=%s warning_percent=%s critical_percent=%s\n' \
  "$status" \
  "$usage_percent" \
  "$active" \
  "$allocated" \
  "$unused" \
  "$maximum" \
  "$warning_percent" \
  "$critical_percent"
exit "$exit_code"
