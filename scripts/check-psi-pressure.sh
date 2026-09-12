#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/psi-pressure-common.sh
source "$script_dir/lib/psi-pressure-common.sh"

psi_dir="${1:-${IRLIGHT_PSI_DIR:-/proc/pressure}}"
some_warning_percent="${IRLIGHT_PSI_SOME_WARNING_PERCENT:-25}"
some_critical_percent="${IRLIGHT_PSI_SOME_CRITICAL_PERCENT:-50}"
full_warning_percent="${IRLIGHT_PSI_FULL_WARNING_PERCENT:-5}"
full_critical_percent="${IRLIGHT_PSI_FULL_CRITICAL_PERCENT:-20}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_PSI_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

if ! psi_evaluate_pressure \
  "$psi_dir/cpu" \
  "$psi_dir/memory" \
  "$psi_dir/io" \
  "$some_warning_percent" \
  "$some_critical_percent" \
  "$full_warning_percent" \
  "$full_critical_percent"; then
  unknown "$PSI_REASON"
fi

printf 'IRLIGHT_PSI_PRESSURE status=%s cpu_some_avg10=%s memory_some_avg10=%s memory_full_avg10=%s io_some_avg10=%s io_full_avg10=%s some_warning_percent=%s some_critical_percent=%s full_warning_percent=%s full_critical_percent=%s\n' \
  "$PSI_STATUS" \
  "$PSI_CPU_SOME" \
  "$PSI_MEMORY_SOME" \
  "$PSI_MEMORY_FULL" \
  "$PSI_IO_SOME" \
  "$PSI_IO_FULL" \
  "$PSI_SOME_WARNING" \
  "$PSI_SOME_CRITICAL" \
  "$PSI_FULL_WARNING" \
  "$PSI_FULL_CRITICAL"
exit "$PSI_EXIT_CODE"
