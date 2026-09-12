#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
disk_path="${1:-${IRLIGHT_DISK_PATH:-${STATE_DIR:-/state}}}"
meminfo_path="${2:-${IRLIGHT_MEMINFO_PATH:-/proc/meminfo}}"
loadavg_path="${3:-${IRLIGHT_LOADAVG_PATH:-/proc/loadavg}}"
cpu_count="${4:-${IRLIGHT_CPU_COUNT:-}}"

run_component() {
  local script="$1"
  shift

  local exit_code
  set +e
  bash "$script" "$@" >/dev/null 2>&1
  exit_code=$?
  set -e

  case "$exit_code" in
    0|1|2|3)
      printf '%s\n' "$exit_code"
      ;;
    *)
      printf '3\n'
      ;;
  esac
}

status_for_code() {
  case "$1" in
    0) printf 'OK\n' ;;
    1) printf 'WARNING\n' ;;
    2) printf 'CRITICAL\n' ;;
    *) printf 'UNKNOWN\n' ;;
  esac
}

disk_code="$(run_component "$script_dir/check-disk-pressure.sh" "$disk_path")"
memory_code="$(run_component "$script_dir/check-memory-pressure.sh" "$meminfo_path")"
load_code="$(run_component "$script_dir/check-load-pressure.sh" "$loadavg_path" "$cpu_count")"

disk_status="$(status_for_code "$disk_code")"
memory_status="$(status_for_code "$memory_code")"
load_status="$(status_for_code "$load_code")"

status="OK"
exit_code=0

# A known critical condition must not be hidden by an unrelated UNKNOWN check.
# Otherwise UNKNOWN is fail-closed and takes precedence over WARNING/OK.
if (( disk_code == 2 || memory_code == 2 || load_code == 2 )); then
  status="CRITICAL"
  exit_code=2
elif (( disk_code == 3 || memory_code == 3 || load_code == 3 )); then
  status="UNKNOWN"
  exit_code=3
elif (( disk_code == 1 || memory_code == 1 || load_code == 1 )); then
  status="WARNING"
  exit_code=1
fi

printf 'IRLIGHT_HOST_PRESSURE status=%s disk_status=%s memory_status=%s load_status=%s\n' \
  "$status" \
  "$disk_status" \
  "$memory_status" \
  "$load_status"
exit "$exit_code"
