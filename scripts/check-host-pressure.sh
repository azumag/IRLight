#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
disk_path="${1:-${IRLIGHT_DISK_PATH:-${STATE_DIR:-/state}}}"
meminfo_path="${2:-${IRLIGHT_MEMINFO_PATH:-/proc/meminfo}}"
loadavg_path="${3:-${IRLIGHT_LOADAVG_PATH:-/proc/loadavg}}"
cpu_count="${4:-${IRLIGHT_CPU_COUNT:-}}"
psi_dir="${5:-${IRLIGHT_PSI_DIR:-/proc/pressure}}"
file_nr_path="${6:-${IRLIGHT_FILE_NR_PATH:-/proc/sys/fs/file-nr}}"
conntrack_count_path="${7:-${IRLIGHT_CONNTRACK_COUNT_PATH:-/proc/sys/net/netfilter/nf_conntrack_count}}"
conntrack_max_path="${8:-${IRLIGHT_CONNTRACK_MAX_PATH:-/proc/sys/net/netfilter/nf_conntrack_max}}"
threads_max_path="${9:-${IRLIGHT_THREADS_MAX_PATH:-/proc/sys/kernel/threads-max}}"

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

component_names=()
component_codes=()

add_component() {
  local name="$1"
  shift

  component_names+=("$name")
  component_codes+=("$(run_component "$@")")
}

# Keep each component registered exactly once. The same registry drives both
# overall severity aggregation and the per-component output fields below, so a
# newly added check cannot accidentally participate in only one of them.
add_component "disk" "$script_dir/check-disk-pressure.sh" "$disk_path"
add_component "memory" "$script_dir/check-memory-pressure.sh" "$meminfo_path"
add_component "load" "$script_dir/check-load-pressure.sh" "$loadavg_path" "$cpu_count"
add_component "psi" "$script_dir/check-psi-pressure.sh" "$psi_dir"
add_component "file_handle" "$script_dir/check-file-handle-pressure.sh" "$file_nr_path"
add_component "conntrack" "$script_dir/check-conntrack-pressure.sh" "$conntrack_count_path" "$conntrack_max_path"
add_component "task" "$script_dir/check-task-pressure.sh" "$loadavg_path" "$threads_max_path"

component_statuses=()
overall_code=0

for code in "${component_codes[@]}"; do
  component_statuses+=("$(status_for_code "$code")")

  # A known critical condition must not be hidden by an unrelated UNKNOWN
  # check. Otherwise UNKNOWN is fail-closed and takes precedence over
  # WARNING/OK. This yields CRITICAL > UNKNOWN > WARNING > OK.
  case "$code" in
    2)
      overall_code=2
      ;;
    3)
      if (( overall_code != 2 )); then
        overall_code=3
      fi
      ;;
    1)
      if (( overall_code == 0 )); then
        overall_code=1
      fi
      ;;
  esac
done

overall_status="$(status_for_code "$overall_code")"
printf 'IRLIGHT_HOST_PRESSURE status=%s' "$overall_status"
for index in "${!component_names[@]}"; do
  printf ' %s_status=%s' "${component_names[$index]}" "${component_statuses[$index]}"
done
printf '\n'
exit "$overall_code"
