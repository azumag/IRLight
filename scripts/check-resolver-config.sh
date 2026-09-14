#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

resolver_config_path="${1:-${IRLIGHT_RESOLVER_CONFIG_PATH:-/etc/resolv.conf}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_RESOLVER_CONFIG_HEALTH status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

critical() {
  local reason="$1"
  printf 'IRLIGHT_RESOLVER_CONFIG_HEALTH status=CRITICAL reason=%s nameserver_count=0\n' "$reason"
  exit 2
}

is_ipv4_literal() {
  local value="$1"
  local IFS=.
  local -a octets
  read -r -a octets <<<"$value"
  (( ${#octets[@]} == 4 )) || return 1

  local octet
  for octet in "${octets[@]}"; do
    [[ "$octet" =~ ^[0-9]{1,3}$ ]] || return 1
    (( 10#$octet <= 255 )) || return 1
  done
}

ipv6_unit_count() {
  local part="$1"
  local allow_ipv4_last="$2"
  local count=0
  local -a fields

  if [[ -z "$part" ]]; then
    REPLY=0
    return 0
  fi

  IFS=: read -r -a fields <<<"$part"
  local index field
  for index in "${!fields[@]}"; do
    field="${fields[$index]}"
    [[ -n "$field" ]] || return 1
    if [[ "$field" == *.* ]]; then
      [[ "$allow_ipv4_last" == true ]] || return 1
      (( index == ${#fields[@]} - 1 )) || return 1
      is_ipv4_literal "$field" || return 1
      (( count += 2 ))
    else
      [[ "$field" =~ ^[0-9A-Fa-f]{1,4}$ ]] || return 1
      (( count += 1 ))
    fi
  done

  REPLY="$count"
}

is_ipv6_literal() {
  local value="$1"
  local address="$value"
  local zone=""

  if [[ "$value" == *%* ]]; then
    address="${value%%\%*}"
    zone="${value#*\%}"
    [[ -n "$address" && -n "$zone" ]] || return 1
    [[ "$zone" =~ ^[A-Za-z0-9_.-]+$ ]] || return 1
  fi

  [[ "$address" == *:* ]] || return 1
  [[ "$address" =~ ^[0-9A-Fa-f:.]+$ ]] || return 1
  [[ "$address" != *:::* ]] || return 1

  if [[ "$address" == *::* ]]; then
    local left="${address%%::*}"
    local right="${address#*::}"
    [[ "$right" != *::* ]] || return 1

    ipv6_unit_count "$left" false || return 1
    local left_units="$REPLY"
    ipv6_unit_count "$right" true || return 1
    local right_units="$REPLY"
    (( left_units + right_units < 8 )) || return 1
  else
    ipv6_unit_count "$address" true || return 1
    (( REPLY == 8 )) || return 1
  fi
}

is_nameserver_address() {
  local value="$1"
  is_ipv4_literal "$value" || is_ipv6_literal "$value"
}

if [[ ! -r "$resolver_config_path" ]]; then
  unknown resolver_config_unavailable
fi

nameserver_count=0
while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
  line="${raw_line%%#*}"
  line="${line%%;*}"

  directive=""
  value=""
  extra=""
  read -r directive value extra <<<"$line" || true
  [[ -z "$directive" ]] && continue
  [[ "$directive" != "nameserver" ]] && continue

  if [[ -z "$value" || -n "$extra" ]]; then
    unknown invalid_nameserver_record
  fi
  if ! is_nameserver_address "$value"; then
    unknown invalid_nameserver_address
  fi

  (( nameserver_count += 1 ))
done < "$resolver_config_path" || unknown resolver_config_unavailable

if (( nameserver_count == 0 )); then
  critical nameserver_missing
fi

printf 'IRLIGHT_RESOLVER_CONFIG_HEALTH status=OK nameserver_count=%s\n' "$nameserver_count"
exit 0
