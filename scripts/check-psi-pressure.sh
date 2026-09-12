#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

psi_dir="${1:-${IRLIGHT_PSI_DIR:-/proc/pressure}}"
some_warning_percent="${IRLIGHT_PSI_SOME_WARNING_PERCENT:-25}"
some_critical_percent="${IRLIGHT_PSI_SOME_CRITICAL_PERCENT:-50}"
full_warning_percent="${IRLIGHT_PSI_FULL_WARNING_PERCENT:-5}"
full_critical_percent="${IRLIGHT_PSI_FULL_CRITICAL_PERCENT:-20}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_PSI_PRESSURE status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

is_uint() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

normalize_threshold() {
  local value="$1"
  if ! is_uint "$value" || (( ${#value} > 3 )); then
    return 1
  fi
  value=$((10#$value))
  if (( value < 0 || value > 100 )); then
    return 1
  fi
  printf '%s\n' "$value"
}

if ! some_warning_percent="$(normalize_threshold "$some_warning_percent")" || \
   ! some_critical_percent="$(normalize_threshold "$some_critical_percent")" || \
   ! full_warning_percent="$(normalize_threshold "$full_warning_percent")" || \
   ! full_critical_percent="$(normalize_threshold "$full_critical_percent")"; then
  unknown invalid_threshold
fi
if (( some_warning_percent >= some_critical_percent || full_warning_percent >= full_critical_percent )); then
  unknown invalid_threshold
fi

percent_to_basis_points() {
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

read_avg10() {
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
    if ! percent_to_basis_points "$value" >/dev/null; then
      return 1
    fi
  done
  printf '%s\n' "$avg10"
}

for resource in cpu memory io; do
  if [[ ! -r "$psi_dir/$resource" ]]; then
    unknown psi_unavailable
  fi
done

if ! cpu_some="$(read_avg10 "$psi_dir/cpu" some)"; then
  unknown invalid_cpu_psi
fi
if ! memory_some="$(read_avg10 "$psi_dir/memory" some)" || \
   ! memory_full="$(read_avg10 "$psi_dir/memory" full)"; then
  unknown invalid_memory_psi
fi
if ! io_some="$(read_avg10 "$psi_dir/io" some)" || \
   ! io_full="$(read_avg10 "$psi_dir/io" full)"; then
  unknown invalid_io_psi
fi

cpu_some_bp="$(percent_to_basis_points "$cpu_some")" || unknown invalid_cpu_psi
memory_some_bp="$(percent_to_basis_points "$memory_some")" || unknown invalid_memory_psi
memory_full_bp="$(percent_to_basis_points "$memory_full")" || unknown invalid_memory_psi
io_some_bp="$(percent_to_basis_points "$io_some")" || unknown invalid_io_psi
io_full_bp="$(percent_to_basis_points "$io_full")" || unknown invalid_io_psi

some_max_bp="$cpu_some_bp"
for value in "$memory_some_bp" "$io_some_bp"; do
  if (( value > some_max_bp )); then
    some_max_bp="$value"
  fi
done
full_max_bp="$memory_full_bp"
if (( io_full_bp > full_max_bp )); then
  full_max_bp="$io_full_bp"
fi

status="OK"
exit_code=0
if (( some_max_bp >= some_critical_percent * 100 || full_max_bp >= full_critical_percent * 100 )); then
  status="CRITICAL"
  exit_code=2
elif (( some_max_bp >= some_warning_percent * 100 || full_max_bp >= full_warning_percent * 100 )); then
  status="WARNING"
  exit_code=1
fi

printf 'IRLIGHT_PSI_PRESSURE status=%s cpu_some_avg10=%s memory_some_avg10=%s memory_full_avg10=%s io_some_avg10=%s io_full_avg10=%s some_warning_percent=%s some_critical_percent=%s full_warning_percent=%s full_critical_percent=%s\n' \
  "$status" \
  "$cpu_some" \
  "$memory_some" \
  "$memory_full" \
  "$io_some" \
  "$io_full" \
  "$some_warning_percent" \
  "$some_critical_percent" \
  "$full_warning_percent" \
  "$full_critical_percent"
exit "$exit_code"
