#!/usr/bin/env bash

# Shared, side-effect-free helpers for scalar pressure checks.
# Callers own their output prefix, UNKNOWN reason strings, paths, and policy.

pressure_is_uint() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

pressure_normalize_int64_uint() {
  local value="$1"
  if ! pressure_is_uint "$value"; then
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
  PRESSURE_VALUE="$value"
}

pressure_normalize_threshold_pair() {
  local warning_raw="$1"
  local critical_raw="$2"

  for threshold in "$warning_raw" "$critical_raw"; do
    if ! pressure_is_uint "$threshold" || (( ${#threshold} > 3 )); then
      return 1
    fi
  done

  # Force decimal so values such as 080 are not interpreted as octal by Bash.
  local warning=$((10#$warning_raw))
  local critical=$((10#$critical_raw))
  if (( warning > 100 || critical > 100 || warning >= critical )); then
    return 1
  fi

  PRESSURE_WARNING_PERCENT="$warning"
  PRESSURE_CRITICAL_PERCENT="$critical"
}

pressure_usage_percent() {
  local current="$1"
  local maximum="$2"

  if ! pressure_normalize_int64_uint "$current"; then
    return 1
  fi
  current="$PRESSURE_VALUE"
  if ! pressure_normalize_int64_uint "$maximum"; then
    return 1
  fi
  maximum="$PRESSURE_VALUE"

  current=$((10#$current))
  maximum=$((10#$maximum))
  if (( maximum <= 0 || current > maximum )); then
    return 1
  fi

  local usage_percent
  usage_percent="$(
    awk -v current="$current" -v maximum="$maximum" \
      'BEGIN { printf "%d", (current * 100.0) / maximum }'
  )" || return 1
  if ! pressure_is_uint "$usage_percent" || (( ${#usage_percent} > 3 )); then
    return 1
  fi
  usage_percent=$((10#$usage_percent))
  if (( usage_percent > 100 )); then
    return 1
  fi

  PRESSURE_VALUE="$usage_percent"
}
