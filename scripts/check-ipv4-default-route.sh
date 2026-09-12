#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

interface_name="${1:-${IRLIGHT_NETWORK_INTERFACE:-}}"
route_table="${2:-${IRLIGHT_IPV4_ROUTE_TABLE:-/proc/net/route}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_IPV4_DEFAULT_ROUTE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

critical() {
  local reason="$1"
  printf 'IRLIGHT_IPV4_DEFAULT_ROUTE status=CRITICAL reason=%s\n' "$reason"
  exit 2
}

if [[ -z "$interface_name" ]]; then
  unknown target_required
fi
if [[ "$interface_name" == *[[:space:]/]* ]]; then
  unknown invalid_target
fi
if [[ -z "$route_table" || ! -r "$route_table" ]]; then
  unknown route_table_unavailable
fi

exec 3< "$route_table" || unknown route_table_unavailable

header=""
if ! IFS= read -r header <&3; then
  unknown invalid_route_table
fi
read -r h_iface h_destination h_gateway h_flags h_refcnt h_use h_metric h_mask h_mtu h_window h_irtt h_extra <<< "$header"
if [[ "$h_iface" != "Iface" || "$h_destination" != "Destination" || "$h_gateway" != "Gateway" || "$h_flags" != "Flags" || "$h_refcnt" != "RefCnt" || "$h_use" != "Use" || "$h_metric" != "Metric" || "$h_mask" != "Mask" || "$h_mtu" != "MTU" || "$h_window" != "Window" || "$h_irtt" != "IRTT" || -n "${h_extra:-}" ]]; then
  unknown invalid_route_table
fi

target_malformed=0
default_seen=0

while IFS= read -r line <&3 || [[ -n "$line" ]]; do
  [[ -z "$line" ]] && continue

  read -r iface destination gateway flags refcnt use metric mask mtu window irtt extra <<< "$line"
  [[ "$iface" == "$interface_name" ]] || continue

  if [[ -z "${destination:-}" || -z "${gateway:-}" || -z "${flags:-}" || -z "${refcnt:-}" || -z "${use:-}" || -z "${metric:-}" || -z "${mask:-}" || -z "${mtu:-}" || -z "${window:-}" || -z "${irtt:-}" || -n "${extra:-}" ]]; then
    target_malformed=1
    continue
  fi
  if [[ ! "$destination" =~ ^[0-9A-Fa-f]{8}$ || ! "$mask" =~ ^[0-9A-Fa-f]{8}$ || ! "$flags" =~ ^[0-9A-Fa-f]{1,8}$ ]]; then
    target_malformed=1
    continue
  fi

  if [[ "$destination" != "00000000" || "$mask" != "00000000" ]]; then
    continue
  fi

  default_seen=1
  flags_value=$((16#$flags))
  if (( (flags_value & 0x1) != 0 && (flags_value & 0x200) == 0 )); then
    printf 'IRLIGHT_IPV4_DEFAULT_ROUTE status=OK route=default\n'
    exit 0
  fi
done

if (( target_malformed != 0 )); then
  unknown invalid_target_route
fi
if (( default_seen != 0 )); then
  critical default_route_unusable
fi
critical default_route_missing
