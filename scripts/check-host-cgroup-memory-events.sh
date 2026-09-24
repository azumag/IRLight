#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-}"
baseline_path="${2:-}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_CGROUP_MEMORY_EVENTS status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

# memory.events counters are workload-cgroup specific and cumulative. Host
# aggregation must never guess a cgroup or generation. Require the current file
# and an operator-managed same-generation baseline explicitly.
if [[ -z "$current_path" || -z "$baseline_path" ]]; then
  unknown target_not_configured
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
checker="$script_dir/check-cgroup-memory-events.sh"
if [[ ! -r "$checker" ]]; then
  unknown checker_unavailable
fi

exec bash "$checker" "$current_path" "$baseline_path"
