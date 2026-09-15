#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
interface_name="${1:-${IRLIGHT_NETWORK_INTERFACE:-}}"
address_family="${2:-${IRLIGHT_NETWORK_ADDRESS_FAMILY:-}}"
interface_dir="${3:-${IRLIGHT_NETWORK_INTERFACE_DIR:-}}"
ipv4_route_table="${4:-${IRLIGHT_IPV4_ROUTE_TABLE:-/proc/net/route}}"
ipv6_route_table="${5:-${IRLIGHT_IPV6_ROUTE_TABLE:-/proc/net/ipv6_route}}"
component_timeout_seconds="${IRLIGHT_NETWORK_COMPONENT_TIMEOUT_SECONDS:-10}"
interface_errors_mode="${IRLIGHT_NETWORK_INTERFACE_ERRORS_MODE:-disabled}"
interface_errors_baseline_dir="${IRLIGHT_NETWORK_STATS_BASELINE_DIR:-}"
udp_snmp_errors_mode="${IRLIGHT_UDP_SNMP_ERRORS_MODE:-disabled}"
udp_snmp_path="${IRLIGHT_UDP_SNMP_PATH:-/proc/net/snmp}"
udp_snmp_baseline_path="${IRLIGHT_UDP_SNMP_BASELINE_PATH:-}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_NETWORK_EGRESS_HEALTH status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

if [[ -z "$interface_name" || "$interface_name" == *[[:space:]/]* ]]; then
  unknown invalid_target
fi

case "$address_family" in
  ipv4|ipv6|dual)
    ;;
  "")
    unknown address_family_required
    ;;
  *)
    unknown invalid_address_family
    ;;
esac

if [[ -z "$interface_dir" ]]; then
  interface_dir="/sys/class/net/$interface_name"
fi

component_timeout_valid=true
if [[ ! "$component_timeout_seconds" =~ ^[0-9]+$ ]] || (( ${#component_timeout_seconds} > 3 )); then
  component_timeout_valid=false
else
  component_timeout_seconds=$((10#$component_timeout_seconds))
  if (( component_timeout_seconds < 1 || component_timeout_seconds > 300 )); then
    component_timeout_valid=false
  fi
fi

run_component() {
  local script="$1"
  shift

  # Keep the aggregate bounded even if a proc/sysfs-backed component wedges.
  # Invalid timeout configuration or a missing timeout utility must fail closed
  # instead of silently falling back to an unbounded component execution.
  if [[ "$component_timeout_valid" != true ]] || ! command -v timeout >/dev/null 2>&1; then
    printf '3\n'
    return
  fi

  local exit_code
  set +e
  timeout --signal=TERM --kill-after=2s "${component_timeout_seconds}s" \
    bash "$script" "$@" >/dev/null 2>&1
  exit_code=$?
  set -e

  case "$exit_code" in
    0|1|2|3)
      printf '%s\n' "$exit_code"
      ;;
    *)
      printf '3\n'
      ;;
  esac
}

status_for_code() {
  case "$1" in
    0) printf 'OK\n' ;;
    1) printf 'WARNING\n' ;;
    2) printf 'CRITICAL\n' ;;
    *) printf 'UNKNOWN\n' ;;
  esac
}

merge_code() {
  local current="$1"
  local candidate="$2"

  # Preserve the operational severity contract used by the host aggregate:
  # CRITICAL > UNKNOWN > WARNING > OK. A confirmed outage must not be hidden
  # by an unrelated parse/availability failure.
  case "$candidate" in
    2)
      printf '2\n'
      ;;
    3)
      if (( current == 2 )); then
        printf '%s\n' "$current"
      else
        printf '3\n'
      fi
      ;;
    1)
      if (( current == 0 )); then
        printf '1\n'
      else
        printf '%s\n' "$current"
      fi
      ;;
    *)
      printf '%s\n' "$current"
      ;;
  esac
}

link_code="$(run_component "$script_dir/check-network-link-health.sh" "$interface_dir")"
overall_code="$link_code"
ipv4_status="NOT_REQUIRED"
ipv6_status="NOT_REQUIRED"
optional_status_fields=()

register_optional_status() {
  local field_name="$1"
  local exit_code="$2"

  # Keep status rendering and severity aggregation coupled so future opt-in
  # components cannot accidentally update one side without the other.
  optional_status_fields+=("${field_name}=$(status_for_code "$exit_code")")
  overall_code="$(merge_code "$overall_code" "$exit_code")"
}

if [[ "$address_family" == "ipv4" || "$address_family" == "dual" ]]; then
  ipv4_code="$(run_component "$script_dir/check-ipv4-default-route.sh" "$interface_name" "$ipv4_route_table")"
  ipv4_status="$(status_for_code "$ipv4_code")"
  overall_code="$(merge_code "$overall_code" "$ipv4_code")"
fi

if [[ "$address_family" == "ipv6" || "$address_family" == "dual" ]]; then
  ipv6_code="$(run_component "$script_dir/check-ipv6-default-route.sh" "$interface_name" "$ipv6_route_table")"
  ipv6_status="$(status_for_code "$ipv6_code")"
  overall_code="$(merge_code "$overall_code" "$ipv6_code")"
fi

case "$interface_errors_mode" in
  disabled)
    ;;
  enabled)
    interface_errors_code="$(run_component \
      "$script_dir/check-network-interface-errors.sh" \
      "$interface_dir/statistics" \
      "$interface_errors_baseline_dir")"
    register_optional_status "interface_errors_status" "$interface_errors_code"
    ;;
  *)
    register_optional_status "interface_errors_status" 3
    ;;
esac

case "$udp_snmp_errors_mode" in
  disabled)
    ;;
  enabled)
    udp_snmp_errors_code="$(run_component \
      "$script_dir/check-udp-snmp-errors.sh" \
      "$udp_snmp_path" \
      "$udp_snmp_baseline_path")"
    register_optional_status "udp_snmp_errors_status" "$udp_snmp_errors_code"
    ;;
  *)
    register_optional_status "udp_snmp_errors_status" 3
    ;;
esac

output_fields=(
  "IRLIGHT_NETWORK_EGRESS_HEALTH"
  "status=$(status_for_code "$overall_code")"
  "link_status=$(status_for_code "$link_code")"
  "ipv4_route_status=$ipv4_status"
  "ipv6_route_status=$ipv6_status"
)
output_fields+=("${optional_status_fields[@]}")
output_fields+=("family=$address_family")
printf '%s\n' "${output_fields[*]}"
exit "$overall_code"
