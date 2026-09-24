#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-}"
maximum_path="${2:-}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_CGROUP_PID_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

# The host aggregate must never guess which cgroup represents the service or
# container being monitored. Require both control files explicitly so enabling
# the signal without deployment-owned targeting fails closed.
if [[ -z "$current_path" || -z "$maximum_path" ]]; then
  unknown target_not_configured
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
checker="$script_dir/check-cgroup-pid-pressure.sh"
if [[ ! -r "$checker" ]]; then
  unknown checker_unavailable
fi

exec bash "$checker" "$current_path" "$maximum_path"
