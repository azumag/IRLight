#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-${IRLIGHT_CGROUP_CPU_STAT_PATH:-}}"
baseline_path="${2:-${IRLIGHT_CGROUP_CPU_STAT_BASELINE_PATH:-}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_CGROUP_CPU_THROTTLING status=UNKNOWN reason=%s\n' "$reason"
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

parse_cpu_stat() {
  local path="$1"
  local array_name="$2"
  local path_reason="$3"
  local -n target="$array_name"
  local -a lines=()
  local line key value extra canonical

  [[ -n "$path" && -f "$path" && -r "$path" ]] || unknown "$path_reason"
  mapfile -t lines < "$path" || unknown "$path_reason"

  for line in "${lines[@]}"; do
    [[ -n "$line" ]] || continue
    key=""
    value=""
    extra=""
    read -r key value extra <<<"$line" || unknown invalid_cpu_stat_record
    if [[ -z "${key:-}" || -z "${value:-}" || -n "${extra:-}" ]] || ! is_uint64_bounded "$value"; then
      unknown invalid_cpu_stat_record
    fi
    canonical=$((10#$value))
    case "$key" in
      nr_periods|nr_throttled|throttled_usec)
        [[ -z "${target[$key]+x}" ]] || unknown duplicate_cpu_stat_record
        target[$key]="$canonical"
        ;;
      *)
        # cpu.stat may gain counters on newer kernels. Validate record shape
        # and numeric bounds, but ignore counters this diagnostic does not
        # interpret so a kernel extension does not become a false outage.
        ;;
    esac
  done

  for key in nr_periods nr_throttled throttled_usec; do
    [[ -n "${target[$key]+x}" ]] || unknown missing_required_counter
  done
}

declare -A current=()
declare -A baseline=()
parse_cpu_stat "$current_path" current current_cpu_stat_unavailable
parse_cpu_stat "$baseline_path" baseline baseline_cpu_stat_unavailable

declare -A delta=()
for key in nr_periods nr_throttled throttled_usec; do
  if (( current[$key] < baseline[$key] )); then
    unknown counter_reset
  fi
  delta[$key]=$(( current[$key] - baseline[$key] ))
done

status="OK"
reason="none"
exit_code=0
if (( delta[nr_throttled] > 0 || delta[throttled_usec] > 0 )); then
  # cpu.stat alone has no wall-clock sampling interval or media impact signal,
  # so confirmed quota throttling is a WARNING. Escalation to CRITICAL belongs
  # to PSI/media symptoms or an operator policy with a known sampling window.
  status="WARNING"
  reason="cpu_throttling_activity"
  exit_code=1
fi

printf 'IRLIGHT_CGROUP_CPU_THROTTLING status=%s reason=%s nr_periods_delta=%s nr_throttled_delta=%s throttled_usec_delta=%s\n' \
  "$status" \
  "$reason" \
  "${delta[nr_periods]}" \
  "${delta[nr_throttled]}" \
  "${delta[throttled_usec]}"
exit "$exit_code"
