#!/usr/bin/env bash
set -euo pipefail

protocol="${1:-${NETEM_PROTOCOL:-}}"
profile_seconds="${NETEM_PROFILE_SECONDS:-12}"
profiles="latency-50,latency-100,latency-300,latency-1000"

case "$protocol" in
  rtmp)
    source_script="scripts/smoke-rtmp-netem-degradation-matrix.sh"
    ;;
  srt)
    source_script="scripts/smoke-srt-netem-degradation-matrix.sh"
    ;;
  *)
    echo "usage: $0 <rtmp|srt>" >&2
    exit 2
    ;;
esac

case "$profile_seconds" in
  ''|*[!0-9]*)
    echo "NETEM_PROFILE_SECONDS must be a positive integer" >&2
    exit 2
    ;;
esac
if (( profile_seconds < 5 || profile_seconds > 60 )); then
  echo "NETEM_PROFILE_SECONDS must be between 5 and 60" >&2
  exit 2
fi

[[ -f "$source_script" ]] || {
  echo "matrix source script not found: $source_script" >&2
  exit 2
}

tmp_script="$(mktemp)"
log_file="$(mktemp)"
cleanup() {
  rm -f "$tmp_script" "$log_file"
}
trap cleanup EXIT

python3 - "$source_script" "$tmp_script" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1])
target = Path(sys.argv[2])
text = source.read_text()
needle = "    latency-jitter) printf '%s\\n' '--delay-ms' '250' '--jitter-ms' '100' ;;"
replacement = "\n".join(
    [
        "    latency-50) printf '%s\\n' '--delay-ms' '50' ;;",
        "    latency-100) printf '%s\\n' '--delay-ms' '100' ;;",
        "    latency-300) printf '%s\\n' '--delay-ms' '300' ;;",
        "    latency-1000) printf '%s\\n' '--delay-ms' '1000' ;;",
        needle,
    ]
)
if text.count(needle) != 1:
    raise SystemExit(
        f"expected exactly one latency-jitter profile in {source}, found {text.count(needle)}"
    )
target.write_text(text.replace(needle, replacement))
PY

bash -n "$tmp_script"

printf '\n=== netem-latency-levels protocol=%s profiles=%s duration=%ss ===\n' \
  "$protocol" "$profiles" "$profile_seconds"

NETEM_MATRIX_PROFILES="$profiles" \
NETEM_PROFILE_SECONDS="$profile_seconds" \
  bash "$tmp_script" | tee "$log_file"

IFS=',' read -r -a expected_profiles <<<"$profiles"
for profile in "${expected_profiles[@]}"; do
  if ! grep -Eq "profile=${profile} .*result=PASS" "$log_file"; then
    echo "missing PASS result for protocol=$protocol profile=$profile" >&2
    exit 1
  fi
  printf 'protocol=%s profile=%s result=PASS\n' "$protocol" "$profile"
done

echo "RTMP/SRT latency level harness passed protocol=$protocol profiles=$profiles duration=${profile_seconds}s"
