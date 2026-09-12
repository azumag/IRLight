#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

interface_dir="${1:-${IRLIGHT_NETWORK_INTERFACE_DIR:-}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_NETWORK_LINK_HEALTH status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

if [[ -z "$interface_dir" ]]; then
  unknown target_required
fi

operstate_path="$interface_dir/operstate"
if [[ ! -d "$interface_dir" || ! -r "$operstate_path" ]]; then
  unknown operstate_unavailable
fi

state=""
line_count=0
while IFS= read -r line || [[ -n "$line" ]]; do
  line_count=$((line_count + 1))
  state="$line"
done < "$operstate_path" || unknown operstate_unavailable

if (( line_count != 1 )); then
  unknown invalid_operstate
fi

case "$state" in
  up)
    printf 'IRLIGHT_NETWORK_LINK_HEALTH status=OK operstate=up\n'
    exit 0
    ;;
  dormant|testing)
    printf 'IRLIGHT_NETWORK_LINK_HEALTH status=WARNING operstate=%s\n' "$state"
    exit 1
    ;;
  down|lowerlayerdown|notpresent)
    printf 'IRLIGHT_NETWORK_LINK_HEALTH status=CRITICAL operstate=%s\n' "$state"
    exit 2
    ;;
  unknown)
    unknown operstate_unknown
    ;;
  *)
    unknown invalid_operstate
    ;;
esac
