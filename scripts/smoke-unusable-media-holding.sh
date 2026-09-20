#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/irlight-unusable-media-holding-wrapper.XXXXXX")"
raw_log="$tmp_dir/unusable-media-holding.raw.log"
core_pid=""

cleanup_wrapper() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "$core_pid" ]] && kill -0 "$core_pid" 2>/dev/null; then
    kill -TERM "$core_pid" 2>/dev/null || true
    wait "$core_pid" 2>/dev/null || true
  fi
  rm -rf "$tmp_dir"
  exit "$status"
}

forward_signal() {
  local exit_status="$1"
  trap - INT TERM
  if [[ -n "$core_pid" ]] && kill -0 "$core_pid" 2>/dev/null; then
    kill -TERM "$core_pid" 2>/dev/null || true
    wait "$core_pid" 2>/dev/null || true
    core_pid=""
  fi
  exit "$exit_status"
}

trap cleanup_wrapper EXIT
trap 'forward_signal 130' INT
trap 'forward_signal 143' TERM

bash "$script_dir/smoke-unusable-media-holding-core.sh" >"$raw_log" 2>&1 &
core_pid=$!
set +e
wait "$core_pid"
status=$?
set -e
core_pid=""

if (( status == 0 )); then
  echo "IRLight unusable-media HOLDING smoke passed."
  exit 0
fi

echo "::error title=IRLight unusable media holding smoke failure::stage=unusable-media-holding-quarantined; inner diagnostics withheld because this smoke carries generated ingest credentials and session material" >&2
exit "$status"
