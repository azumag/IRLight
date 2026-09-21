#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs="${IRLIGHT_EGRESS_RECONNECT_STRESS_RUNS:-10}"
scenario="${IRLIGHT_EGRESS_RECONNECT_STRESS_SCENARIO:-both}"
diagnostic_tmp_dir=""

cleanup() {
  if [[ -n "$diagnostic_tmp_dir" ]]; then
    rm -rf "$diagnostic_tmp_dir"
  fi
}
trap cleanup EXIT

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

diagnostic_tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/irlight-egress-reconnect-stress.XXXXXX")"

emit_stack_fingerprint() {
  local scenario_name="$1"
  local iteration="$2"
  local log_file="$3"
  local fingerprint

  # The raw smoke log is temporary and is never copied to the durable artifact.
  # Reuse the same allowlist-only, byte-bounded extractor as the shared Docker
  # suite so a stress-only recurrence leaves useful evidence without retaining
  # destination URLs, stream keys, source lines, or arbitrary traceback text.
  # An extractor failure cannot prove the input was completely classified, so
  # report the fixed incomplete-evidence fallback rather than capped=no.
  fingerprint="$(
    python3 "$repo_root/scripts/extract-egress-stack-fingerprint.py" <"$log_file" 2>/dev/null || \
      printf '%s' 'IRLIGHT_EGRESS_STACK_FINGERPRINT stack_fingerprint=UNAVAILABLE capped=yes'
  )"
  printf '%s\n' "$fingerprint" >&2

  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
      echo
      echo '#### Egress reconnect stress failure fingerprint'
      echo
      printf 'scenario=`%s` iteration=`%s`\n\n' "$scenario_name" "$iteration"
      printf '`%s`\n' "$fingerprint"
    } >>"$GITHUB_STEP_SUMMARY"
  fi
}

run_smoke() {
  local scenario_name="$1"
  local smoke_name="$2"
  local smoke_path="$repo_root/scripts/$smoke_name"
  local iteration log_file

  if [[ ! -f "$smoke_path" ]]; then
    printf 'IRLIGHT_EGRESS_STRESS status=ERROR scenario=%s reason=smoke_missing\n' "$scenario_name" >&2
    return 2
  fi

  for ((iteration = 1; iteration <= runs; iteration++)); do
    log_file="$diagnostic_tmp_dir/${scenario_name}-${iteration}.log"
    printf 'IRLIGHT_EGRESS_STRESS status=START scenario=%s iteration=%d total=%d\n' \
      "$scenario_name" "$iteration" "$runs"
    # Preserve the existing live workflow output while retaining only a
    # run-local 0600 copy long enough to derive the bounded fingerprint.
    # pipefail makes a smoke failure authoritative even when tee succeeds.
    if bash "$smoke_path" 2>&1 | tee "$log_file"; then
      printf 'IRLIGHT_EGRESS_STRESS status=PASS scenario=%s iteration=%d total=%d\n' \
        "$scenario_name" "$iteration" "$runs"
    else
      emit_stack_fingerprint "$scenario_name" "$iteration" "$log_file"
      printf 'IRLIGHT_EGRESS_STRESS status=FAIL scenario=%s iteration=%d total=%d\n' \
        "$scenario_name" "$iteration" "$runs" >&2
      return 1
    fi
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
