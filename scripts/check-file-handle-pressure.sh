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

pressure_normalize_int64_uint "$allocated_raw" || unknown invalid_file_nr
allocated="$PRESSURE_VALUE"
pressure_normalize_int64_uint "$unused_raw" || unknown invalid_file_nr
unused="$PRESSURE_VALUE"
pressure_normalize_int64_uint "$maximum_raw" || unknown invalid_file_nr
maximum="$PRESSURE_VALUE"

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
pressure_usage_percent "$active" "$maximum" || unknown invalid_file_nr
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
