#!/usr/bin/env bash

# Shared helpers for scalar pressure checks. They do not print, exit, or mutate
# runtime state; successful calls return normalized values via PRESSURE_* vars.
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
  local threshold

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

  # Compute floor(current * 100 / maximum) without floating-point rounding and
  # without overflowing signed 64-bit arithmetic. For each integer percent p,
  # current >= ceil(maximum * p / 100) iff p is not greater than the exact
  # percentage. Split maximum into quotient/remainder before multiplying so all
  # intermediates remain <= maximum.
  local maximum_hundredth=$((maximum / 100))
  local maximum_remainder=$((maximum % 100))
  local percent threshold
  for ((percent = 100; percent >= 0; percent--)); do
    threshold=$((
      maximum_hundredth * percent
      + (maximum_remainder * percent + 99) / 100
    ))
    if (( current >= threshold )); then
      PRESSURE_VALUE="$percent"
      return 0
    fi
  done

  return 1
}
