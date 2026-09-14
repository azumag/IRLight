#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-${IRLIGHT_CGROUP_MEMORY_EVENTS_PATH:-}}"
baseline_path="${2:-${IRLIGHT_CGROUP_MEMORY_EVENTS_BASELINE_PATH:-}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_CGROUP_MEMORY_EVENTS status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

is_uint64_bounded() {
  local value="$1"
  [[ "$value" =~ ^[0-9]+$ ]] || return 1
  (( ${#value} <= 19 )) || return 1
  if (( ${#value} == 19 )) && [[ "$value" > "9223372036854775807" ]]; then
    return 1
  fi
  return 0
}

parse_events() {
  local path="$1"
  local array_name="$2"
  local path_reason="$3"
  local -n target="$array_name"
  local key value extra canonical

  [[ -n "$path" && -f "$path" && -r "$path" ]] || unknown "$path_reason"

  while IFS=$' \t' read -r key value extra; do
    [[ -n "${key:-}" ]] || continue
    if [[ -z "${value:-}" || -n "${extra:-}" ]] || ! is_uint64_bounded "$value"; then
      unknown invalid_events_record
    fi
    canonical=$((10#$value))
    case "$key" in
      low|high|max|oom|oom_kill|oom_group_kill)
        [[ -z "${target[$key]+x}" ]] || unknown duplicate_events_record
        target[$key]="$canonical"
        ;;
      *)
        # memory.events may gain counters on newer kernels. Validate the
        # record shape/value, but do not fail readiness just for an unknown
        # future counter that this check does not interpret.
        ;;
    esac
  done < "$path"

  for key in low high max oom oom_kill; do
    [[ -n "${target[$key]+x}" ]] || unknown missing_required_counter
  done
  target[oom_group_kill]="${target[oom_group_kill]:-0}"
}

declare -A current=()
declare -A baseline=()
parse_events "$current_path" current current_events_unavailable
parse_events "$baseline_path" baseline baseline_events_unavailable

declare -A delta=()
for key in low high max oom oom_kill oom_group_kill; do
  if (( current[$key] < baseline[$key] )); then
    unknown counter_reset
  fi
  delta[$key]=$(( current[$key] - baseline[$key] ))
done

status="OK"
reason="none"
exit_code=0
if (( delta[oom] > 0 || delta[oom_kill] > 0 || delta[oom_group_kill] > 0 )); then
  status="CRITICAL"
  reason="oom_activity"
  exit_code=2
elif (( delta[max] > 0 || delta[high] > 0 || delta[low] > 0 )); then
  status="WARNING"
  reason="memory_pressure_activity"
  exit_code=1
fi

printf 'IRLIGHT_CGROUP_MEMORY_EVENTS status=%s reason=%s low_delta=%s high_delta=%s max_delta=%s oom_delta=%s oom_kill_delta=%s oom_group_kill_delta=%s\n' \
  "$status" \
  "$reason" \
  "${delta[low]}" \
  "${delta[high]}" \
  "${delta[max]}" \
  "${delta[oom]}" \
  "${delta[oom_kill]}" \
  "${delta[oom_group_kill]}"
exit "$exit_code"
