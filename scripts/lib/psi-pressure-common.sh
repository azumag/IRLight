#!/usr/bin/env bash

psi_is_uint() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

psi_normalize_threshold() {
  local value="$1"
  if ! psi_is_uint "$value" || (( ${#value} > 3 )); then
    return 1
  fi
  value=$((10#$value))
  if (( value > 100 )); then
    return 1
  fi
  printf '%s\n' "$value"
}

psi_percent_to_basis_points() {
  local value="$1"
  if [[ ! "$value" =~ ^[0-9]{1,3}([.][0-9]{1,6})?$ ]]; then
    return 1
  fi

  local whole="${value%%.*}"
  local fraction=""
  if [[ "$value" == *.* ]]; then
    fraction="${value#*.}"
  fi
  while (( ${#fraction} < 2 )); do
    fraction+="0"
  done
  fraction="${fraction:0:2}"

  whole=$((10#$whole))
  fraction=$((10#${fraction:-0}))
  if (( whole > 100 || (whole == 100 && fraction != 0) )); then
    return 1
  fi
  printf '%s\n' "$((whole * 100 + fraction))"
}

psi_read_avg10() {
  local file="$1"
  local kind="$2"
  local line=""
  local count=0
  local avg10=""
  local avg60=""
  local avg300=""

  while IFS= read -r line; do
    if [[ "$line" =~ ^${kind}[[:space:]]+avg10=([^[:space:]]+)[[:space:]]+avg60=([^[:space:]]+)[[:space:]]+avg300=([^[:space:]]+)[[:space:]]+total=([0-9]{1,20})$ ]]; then
      count=$((count + 1))
      avg10="${BASH_REMATCH[1]}"
      avg60="${BASH_REMATCH[2]}"
      avg300="${BASH_REMATCH[3]}"
    elif [[ "$line" == "$kind "* ]]; then
      return 1
    fi
  done < "$file"

  if (( count != 1 )); then
    return 1
  fi
  for value in "$avg10" "$avg60" "$avg300"; do
    if ! psi_percent_to_basis_points "$value" >/dev/null; then
      return 1
    fi
  done
  printf '%s\n' "$avg10"
}

psi_evaluate_pressure() {
  local cpu_file="$1"
  local memory_file="$2"
  local io_file="$3"
  local some_warning_raw="$4"
  local some_critical_raw="$5"
  local full_warning_raw="$6"
  local full_critical_raw="$7"

  PSI_REASON=""
  PSI_STATUS="UNKNOWN"
  PSI_EXIT_CODE=3
  PSI_CPU_SOME=""
  PSI_MEMORY_SOME=""
  PSI_MEMORY_FULL=""
  PSI_IO_SOME=""
  PSI_IO_FULL=""
  PSI_SOME_WARNING=""
  PSI_SOME_CRITICAL=""
  PSI_FULL_WARNING=""
  PSI_FULL_CRITICAL=""

  if ! PSI_SOME_WARNING="$(psi_normalize_threshold "$some_warning_raw")" || \
     ! PSI_SOME_CRITICAL="$(psi_normalize_threshold "$some_critical_raw")" || \
     ! PSI_FULL_WARNING="$(psi_normalize_threshold "$full_warning_raw")" || \
     ! PSI_FULL_CRITICAL="$(psi_normalize_threshold "$full_critical_raw")"; then
    PSI_REASON="invalid_threshold"
    return 1
  fi
  if (( PSI_SOME_WARNING >= PSI_SOME_CRITICAL || PSI_FULL_WARNING >= PSI_FULL_CRITICAL )); then
    PSI_REASON="invalid_threshold"
    return 1
  fi

  for file in "$cpu_file" "$memory_file" "$io_file"; do
    if [[ ! -r "$file" ]]; then
      PSI_REASON="psi_unavailable"
      return 1
    fi
  done

  if ! PSI_CPU_SOME="$(psi_read_avg10 "$cpu_file" some)"; then
    PSI_REASON="invalid_cpu_psi"
    return 1
  fi
  if ! PSI_MEMORY_SOME="$(psi_read_avg10 "$memory_file" some)" || \
     ! PSI_MEMORY_FULL="$(psi_read_avg10 "$memory_file" full)"; then
    PSI_REASON="invalid_memory_psi"
    return 1
  fi
  if ! PSI_IO_SOME="$(psi_read_avg10 "$io_file" some)" || \
     ! PSI_IO_FULL="$(psi_read_avg10 "$io_file" full)"; then
    PSI_REASON="invalid_io_psi"
    return 1
  fi

  local cpu_some_bp memory_some_bp memory_full_bp io_some_bp io_full_bp
  cpu_some_bp="$(psi_percent_to_basis_points "$PSI_CPU_SOME")" || {
    PSI_REASON="invalid_cpu_psi"
    return 1
  }
  memory_some_bp="$(psi_percent_to_basis_points "$PSI_MEMORY_SOME")" || {
    PSI_REASON="invalid_memory_psi"
    return 1
  }
  memory_full_bp="$(psi_percent_to_basis_points "$PSI_MEMORY_FULL")" || {
    PSI_REASON="invalid_memory_psi"
    return 1
  }
  io_some_bp="$(psi_percent_to_basis_points "$PSI_IO_SOME")" || {
    PSI_REASON="invalid_io_psi"
    return 1
  }
  io_full_bp="$(psi_percent_to_basis_points "$PSI_IO_FULL")" || {
    PSI_REASON="invalid_io_psi"
    return 1
  }

  local some_max_bp="$cpu_some_bp"
  local value
  for value in "$memory_some_bp" "$io_some_bp"; do
    if (( value > some_max_bp )); then
      some_max_bp="$value"
    fi
  done
  local full_max_bp="$memory_full_bp"
  if (( io_full_bp > full_max_bp )); then
    full_max_bp="$io_full_bp"
  fi

  PSI_STATUS="OK"
  PSI_EXIT_CODE=0
  if (( some_max_bp >= PSI_SOME_CRITICAL * 100 || full_max_bp >= PSI_FULL_CRITICAL * 100 )); then
    PSI_STATUS="CRITICAL"
    PSI_EXIT_CODE=2
  elif (( some_max_bp >= PSI_SOME_WARNING * 100 || full_max_bp >= PSI_FULL_WARNING * 100 )); then
    PSI_STATUS="WARNING"
    PSI_EXIT_CODE=1
  fi
  return 0
}
