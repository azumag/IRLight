#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

cgroup_dir="${1:-}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_CGROUP_PSI_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

# cgroup PSI is workload-specific and hierarchical. Host monitoring must never
# guess a root/current cgroup, representative PID, or container path. Require
# the operator-selected cgroup directory explicitly and delegate the actual PSI
# parsing/threshold semantics to the existing standalone checker.
if [[ -z "$cgroup_dir" ]]; then
  unknown target_not_configured
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
checker="$script_dir/check-cgroup-psi-pressure.sh"
helper="$script_dir/lib/psi-pressure-common.sh"
if [[ ! -r "$checker" || ! -r "$helper" ]]; then
  unknown checker_unavailable
fi

exec bash "$checker" "$cgroup_dir"
