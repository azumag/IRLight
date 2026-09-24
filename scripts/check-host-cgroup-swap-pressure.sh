#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-${IRLIGHT_CGROUP_SWAP_CURRENT_PATH:-}}"
maximum_path="${2:-${IRLIGHT_CGROUP_SWAP_MAX_PATH:-}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_HOST_CGROUP_SWAP status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

# The host aggregate must never guess which workload cgroup is authoritative.
# Require both control-file paths explicitly instead of inheriting the standalone
# checker's root-cgroup defaults.
if [[ -z "$current_path" || -z "$maximum_path" ]]; then
  unknown target_not_configured
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
checker="$script_dir/check-cgroup-swap-pressure.sh"

set +e
output="$(bash "$checker" "$current_path" "$maximum_path" 2>/dev/null)"
checker_code=$?
set -e

case "$checker_code" in
  0|1|2|3)
    ;;
  *)
    unknown checker_failed
    ;;
esac

status=""
if [[ "$output" =~ ^IRLIGHT_CGROUP_SWAP_PRESSURE[[:space:]]status=(OK|WARNING|UNKNOWN|CRITICAL)([[:space:]]|$) ]]; then
  status="${BASH_REMATCH[1]}"
else
  unknown malformed_checker_output
fi

expected_code=3
case "$status" in
  OK) expected_code=0 ;;
  WARNING) expected_code=1 ;;
  CRITICAL) expected_code=2 ;;
  UNKNOWN) expected_code=3 ;;
esac

# A disagreement between the machine-readable status and the checker exit code
# is not safe to normalize into health.
if (( checker_code != expected_code )); then
  unknown checker_failed
fi

printf 'IRLIGHT_HOST_CGROUP_SWAP status=%s\n' "$status"
exit "$checker_code"
