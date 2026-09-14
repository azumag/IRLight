#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cgroup_dir="${1:-${IRLIGHT_CGROUP_RUNTIME_DIR:-}}"
events_baseline_path="${2:-${IRLIGHT_CGROUP_MEMORY_EVENTS_BASELINE_PATH:-}}"
component_timeout_seconds="${IRLIGHT_CGROUP_COMPONENT_TIMEOUT_SECONDS:-10}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_CGROUP_RUNTIME_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

if [[ -z "$cgroup_dir" ]]; then
  unknown target_required
fi

component_timeout_valid=true
if [[ ! "$component_timeout_seconds" =~ ^[0-9]+$ ]] || (( ${#component_timeout_seconds} > 3 )); then
  component_timeout_valid=false
else
  component_timeout_seconds=$((10#$component_timeout_seconds))
  if (( component_timeout_seconds < 1 || component_timeout_seconds > 300 )); then
    component_timeout_valid=false
  fi
fi

run_component() {
  local script="$1"
  shift

  # A targeted aggregate must remain bounded even if one procfs/cgroupfs read
  # wedges. Invalid timeout configuration or a missing timeout utility is
  # therefore UNKNOWN rather than an unbounded fallback.
  if [[ "$component_timeout_valid" != true ]] || ! command -v timeout >/dev/null 2>&1; then
    printf '3\n'
    return
  fi

  local exit_code
  set +e
  timeout --signal=TERM --kill-after=2s "${component_timeout_seconds}s" \
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

merge_code() {
  local code="$1"
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
}

memory_max_code="$(run_component \
  "$script_dir/check-cgroup-memory-pressure.sh" \
  "$cgroup_dir/memory.current" \
  "$cgroup_dir/memory.max")"
memory_high_code="$(run_component \
  "$script_dir/check-cgroup-memory-high-pressure.sh" \
  "$cgroup_dir/memory.current" \
  "$cgroup_dir/memory.high")"
pids_code="$(run_component \
  "$script_dir/check-cgroup-pid-pressure.sh" \
  "$cgroup_dir/pids.current" \
  "$cgroup_dir/pids.max")"
psi_code="$(run_component \
  "$script_dir/check-cgroup-psi-pressure.sh" \
  "$cgroup_dir")"

memory_events_status="NOT_CONFIGURED"
memory_events_code=""
if [[ -n "$events_baseline_path" ]]; then
  memory_events_code="$(run_component \
    "$script_dir/check-cgroup-memory-events.sh" \
    "$cgroup_dir/memory.events" \
    "$events_baseline_path")"
  memory_events_status="$(status_for_code "$memory_events_code")"
fi

overall_code=0
merge_code "$memory_max_code"
merge_code "$memory_high_code"
merge_code "$pids_code"
merge_code "$psi_code"
if [[ -n "$memory_events_code" ]]; then
  merge_code "$memory_events_code"
fi

overall_status="$(status_for_code "$overall_code")"
printf 'IRLIGHT_CGROUP_RUNTIME_PRESSURE status=%s memory_max_status=%s memory_high_status=%s pids_status=%s psi_status=%s memory_events_status=%s\n' \
  "$overall_status" \
  "$(status_for_code "$memory_max_code")" \
  "$(status_for_code "$memory_high_code")" \
  "$(status_for_code "$pids_code")" \
  "$(status_for_code "$psi_code")" \
  "$memory_events_status"
exit "$overall_code"
