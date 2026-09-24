#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-}"
maximum_path="${2:-}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_CGROUP_MEMORY_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

# Host aggregation must never guess which service/container cgroup represents
# production workload. Require both control files explicitly so an incomplete
# opt-in cannot silently fall back to the root cgroup or a different hierarchy.
if [[ -z "$current_path" || -z "$maximum_path" ]]; then
  unknown target_not_configured
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
checker="$script_dir/check-cgroup-memory-pressure.sh"
if [[ ! -r "$checker" ]]; then
  unknown checker_unavailable
fi

exec bash "$checker" "$current_path" "$maximum_path"
