#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
umask 077
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/irlight-srt-netem-matrix-wrapper.XXXXXX")"
chmod 700 "$tmp_dir"
raw_log="$tmp_dir/core.log"
core_script="${SRT_NETEM_MATRIX_CORE:-$repo_root/scripts/smoke-srt-netem-degradation-matrix-core.sh}"
selected_profiles="${NETEM_MATRIX_PROFILES:-loss-1,loss-3,loss-5,loss-10,latency-jitter,bandwidth-800k}"
core_pid=""

cleanup_wrapper() {
  local status=$?
  if [[ -n "$core_pid" ]] && kill -0 "$core_pid" 2>/dev/null; then
    kill -TERM "$core_pid" 2>/dev/null || true
    wait "$core_pid" 2>/dev/null || true
  fi
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup_wrapper EXIT

forward_signal() {
  local status="$1"
  if [[ -n "$core_pid" ]] && kill -0 "$core_pid" 2>/dev/null; then
    kill -TERM "$core_pid" 2>/dev/null || true
    wait "$core_pid" 2>/dev/null || true
    core_pid=""
  fi
  exit "$status"
}
trap 'forward_signal 130' INT
trap 'forward_signal 143' TERM

IFS=',' read -r -a profiles <<<"$selected_profiles"
if (( ${#profiles[@]} == 0 )); then
  echo "::error title=IRLight SRT netem matrix failure::stage=srt-netem-matrix-invalid-profiles" >&2
  exit 2
fi

for profile in "${profiles[@]}"; do
  case "$profile" in
    loss-1|loss-3|loss-5|loss-10|latency-jitter|bandwidth-800k|latency-50|latency-100|latency-300|latency-1000)
      ;;
    *)
      echo "::error title=IRLight SRT netem matrix failure::stage=srt-netem-matrix-invalid-profile" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$core_script" ]]; then
  echo "::error title=IRLight SRT netem matrix failure::stage=srt-netem-matrix-core-missing" >&2
  exit 2
fi

set +e
bash "$core_script" >"$raw_log" 2>&1 &
core_pid=$!
wait "$core_pid"
status=$?
core_pid=""
set -e

if (( status != 0 )); then
  echo "::error title=IRLight SRT netem matrix failure::stage=srt-netem-matrix-quarantined; inner diagnostics withheld because this smoke carries generated ingest credentials and session material" >&2
  exit "$status"
fi

for profile in "${profiles[@]}"; do
  printf 'profile=%s result=PASS\n' "$profile"
done
printf 'SRT netem degradation matrix passed profiles=%s\n' "$selected_profiles"
