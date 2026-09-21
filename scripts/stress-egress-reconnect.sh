#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs="${IRLIGHT_EGRESS_RECONNECT_STRESS_RUNS:-10}"
scenario="${IRLIGHT_EGRESS_RECONNECT_STRESS_SCENARIO:-both}"

usage() {
  cat <<'EOF'
Usage: scripts/stress-egress-reconnect.sh [--help]

Manually repeat the existing legacy RTMP reconnect/stop-terminal Docker smokes
so intermittent outage-detection failures can be reproduced with their normal
secret-safe evidence and thread-stack diagnostics.

Environment:
  IRLIGHT_EGRESS_RECONNECT_STRESS_RUNS       iterations per selected scenario (1..50, default 10)
  IRLIGHT_EGRESS_RECONNECT_STRESS_SCENARIO   reconnect|stop-terminal|both (default both)

This helper is intentionally not part of the regular CI smoke suite.
EOF
}

if (( $# > 1 )); then
  printf 'IRLIGHT_EGRESS_STRESS status=ERROR reason=invalid_arguments\n' >&2
  exit 2
fi
if (( $# == 1 )); then
  if [[ "$1" == "--help" || "$1" == "-h" ]]; then
    usage
    exit 0
  fi
  printf 'IRLIGHT_EGRESS_STRESS status=ERROR reason=invalid_arguments\n' >&2
  exit 2
fi

if [[ ! "$runs" =~ ^[0-9]+$ ]] || (( runs < 1 || runs > 50 )); then
  printf 'IRLIGHT_EGRESS_STRESS status=ERROR reason=invalid_runs\n' >&2
  exit 2
fi

case "$scenario" in
  reconnect|stop-terminal|both)
    ;;
  *)
    printf 'IRLIGHT_EGRESS_STRESS status=ERROR reason=invalid_scenario\n' >&2
    exit 2
    ;;
esac

run_smoke() {
  local scenario_name="$1"
  local smoke_name="$2"
  local smoke_path="$repo_root/scripts/$smoke_name"
  local iteration

  if [[ ! -f "$smoke_path" ]]; then
    printf 'IRLIGHT_EGRESS_STRESS status=ERROR scenario=%s reason=smoke_missing\n' "$scenario_name" >&2
    return 2
  fi

  for ((iteration = 1; iteration <= runs; iteration++)); do
    printf 'IRLIGHT_EGRESS_STRESS status=START scenario=%s iteration=%d total=%d\n' \
      "$scenario_name" "$iteration" "$runs"
    if ! bash "$smoke_path"; then
      printf 'IRLIGHT_EGRESS_STRESS status=FAIL scenario=%s iteration=%d total=%d\n' \
        "$scenario_name" "$iteration" "$runs" >&2
      return 1
    fi
    printf 'IRLIGHT_EGRESS_STRESS status=PASS scenario=%s iteration=%d total=%d\n' \
      "$scenario_name" "$iteration" "$runs"
  done
}

case "$scenario" in
  reconnect)
    run_smoke reconnect smoke-egress-reconnect.sh
    ;;
  stop-terminal)
    run_smoke stop-terminal smoke-egress-stop-terminal.sh
    ;;
  both)
    run_smoke reconnect smoke-egress-reconnect.sh
    run_smoke stop-terminal smoke-egress-stop-terminal.sh
    ;;
esac

printf 'IRLIGHT_EGRESS_STRESS status=PASS scenario=%s runs=%d\n' "$scenario" "$runs"
