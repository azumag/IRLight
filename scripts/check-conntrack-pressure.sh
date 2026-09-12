#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

count_path="${1:-${IRLIGHT_CONNTRACK_COUNT_PATH:-/proc/sys/net/netfilter/nf_conntrack_count}}"
max_path="${2:-${IRLIGHT_CONNTRACK_MAX_PATH:-/proc/sys/net/netfilter/nf_conntrack_max}}"
warning_percent="${IRLIGHT_CONNTRACK_WARNING_PERCENT:-80}"
critical_percent="${IRLIGHT_CONNTRACK_CRITICAL_PERCENT:-90}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_CONNTRACK_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

is_uint() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

normalize_int64_uint() {
  local value="$1"
  if ! is_uint "$value"; then
    return 1
  fi
  while [[ ${#value} -gt 1 && ${value:0:1} == "0" ]]; do
    value="${value:1}"
  done
  if (( ${#value} > 19 )); then
    return 1
  fi
  if (( ${#value} == 19 )) && [[ "$value" > "9223372036854775807" ]]; then
    return 1
  fi
  REPLY="$value"
}

read_single_uint() {
  local path="$1"
  local unavailable_reason="$2"
  local invalid_reason="$3"
  local -a lines
  local raw

  if [[ ! -r "$path" ]]; then
    unknown "$unavailable_reason"
  fi
  mapfile -t lines < "$path" || unknown "$unavailable_reason"
  if (( ${#lines[@]} != 1 )); then
    unknown "$invalid_reason"
  fi
  raw="${lines[0]}"
  if [[ "$raw" =~ [[:space:]] ]]; then
    unknown "$invalid_reason"
  fi
  normalize_int64_uint "$raw" || unknown "$invalid_reason"
  REPLY="$REPLY"
}

for threshold in "$warning_percent" "$critical_percent"; do
  if ! is_uint "$threshold" || (( ${#threshold} > 3 )); then
    unknown invalid_threshold
  fi
done

# Force decimal so values such as 080 are not interpreted as octal by Bash.
warning_percent=$((10#$warning_percent))
critical_percent=$((10#$critical_percent))
if (( warning_percent > 100 || critical_percent > 100 || warning_percent >= critical_percent )); then
  unknown invalid_threshold
fi

read_single_uint "$count_path" conntrack_count_unavailable invalid_conntrack_count
count="$REPLY"
read_single_uint "$max_path" conntrack_max_unavailable invalid_conntrack_max
maximum="$REPLY"

count=$((10#$count))
maximum=$((10#$maximum))
if (( maximum <= 0 || count > maximum )); then
  unknown invalid_conntrack_values
fi

usage_percent="$(
  awk -v count="$count" -v maximum="$maximum" \
    'BEGIN { printf "%d", (count * 100.0) / maximum }'
)"
if ! is_uint "$usage_percent" || (( ${#usage_percent} > 3 )); then
  unknown invalid_conntrack_values
fi
usage_percent=$((10#$usage_percent))
if (( usage_percent > 100 )); then
  unknown invalid_conntrack_values
fi

status="OK"
exit_code=0
if (( usage_percent >= critical_percent )); then
  status="CRITICAL"
  exit_code=2
elif (( usage_percent >= warning_percent )); then
  status="WARNING"
  exit_code=1
fi

printf 'IRLIGHT_CONNTRACK_PRESSURE status=%s usage_percent=%s count=%s maximum=%s warning_percent=%s critical_percent=%s\n' \
  "$status" \
  "$usage_percent" \
  "$count" \
  "$maximum" \
  "$warning_percent" \
  "$critical_percent"
exit "$exit_code"
