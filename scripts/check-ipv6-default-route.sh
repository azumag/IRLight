#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

interface_name="${1:-${IRLIGHT_NETWORK_INTERFACE:-}}"
route_table="${2:-${IRLIGHT_IPV6_ROUTE_TABLE:-/proc/net/ipv6_route}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_IPV6_DEFAULT_ROUTE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

critical() {
  local reason="$1"
  printf 'IRLIGHT_IPV6_DEFAULT_ROUTE status=CRITICAL reason=%s\n' "$reason"
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

target_malformed=0
table_malformed=0
default_seen=0
zero_addr="00000000000000000000000000000000"

while IFS= read -r line <&3 || [[ -n "$line" ]]; do
  [[ -z "$line" ]] && continue

  read -r destination destination_prefix source source_prefix next_hop metric refcnt use flags iface extra <<< "$line"

  # /proc/net/ipv6_route has no header and places the interface in the final
  # field. If the record shape is damaged before that field, its ownership
  # cannot be established safely, so remember a table-level parse failure.
  if [[ -z "${destination:-}" || -z "${destination_prefix:-}" || -z "${source:-}" || -z "${source_prefix:-}" || -z "${next_hop:-}" || -z "${metric:-}" || -z "${refcnt:-}" || -z "${use:-}" || -z "${flags:-}" || -z "${iface:-}" || -n "${extra:-}" ]]; then
    if [[ "${iface:-}" == "$interface_name" ]]; then
      target_malformed=1
    else
      table_malformed=1
    fi
    continue
  fi

  [[ "$iface" == "$interface_name" ]] || continue

  if [[ ! "$destination" =~ ^[0-9A-Fa-f]{32}$ || ! "$source" =~ ^[0-9A-Fa-f]{32}$ || ! "$next_hop" =~ ^[0-9A-Fa-f]{32}$ || ! "$destination_prefix" =~ ^[0-9A-Fa-f]{2}$ || ! "$source_prefix" =~ ^[0-9A-Fa-f]{2}$ || ! "$metric" =~ ^[0-9A-Fa-f]{8}$ || ! "$refcnt" =~ ^[0-9A-Fa-f]{8}$ || ! "$use" =~ ^[0-9A-Fa-f]{8}$ || ! "$flags" =~ ^[0-9A-Fa-f]{8}$ ]]; then
    target_malformed=1
    continue
  fi

  destination_prefix_value=$((16#$destination_prefix))
  source_prefix_value=$((16#$source_prefix))
  if (( destination_prefix_value > 128 || source_prefix_value > 128 )); then
    target_malformed=1
    continue
  fi

  # A generic IPv6 default route is ::/0 with no source-prefix restriction.
  # Source-specific routes do not establish default egress for arbitrary
  # local source addresses and therefore are not accepted here.
  if [[ "$destination" != "$zero_addr" || "$destination_prefix" != "00" || "$source" != "$zero_addr" || "$source_prefix" != "00" ]]; then
    continue
  fi

  default_seen=1
  flags_value=$((16#$flags))
  if (( (flags_value & 0x1) != 0 && (flags_value & 0x200) == 0 )); then
    printf 'IRLIGHT_IPV6_DEFAULT_ROUTE status=OK route=default\n'
    exit 0
  fi
done

if (( target_malformed != 0 || table_malformed != 0 )); then
  unknown invalid_route_table
fi
if (( default_seen != 0 )); then
  critical default_route_unusable
fi
critical default_route_missing
