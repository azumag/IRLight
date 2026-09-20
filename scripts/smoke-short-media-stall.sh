#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/irlight-short-media-stall-wrapper.XXXXXX")"
raw_log="$tmp_dir/short-media-stall.raw.log"
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

bash "$script_dir/smoke-short-media-stall-core.sh" >"$raw_log" 2>&1 &
core_pid=$!
set +e
wait "$core_pid"
status=$?
set -e
core_pid=""

if (( status == 0 )); then
  echo "short full-media stall smoke passed."
  exit 0
fi

echo "::error title=IRLight short media stall smoke failure::stage=short-media-stall-quarantined; inner diagnostics withheld because this smoke carries generated ingest credentials and session material" >&2
exit "$status"
