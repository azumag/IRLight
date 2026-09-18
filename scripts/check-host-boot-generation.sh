#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

current_path="${1:-${IRLIGHT_BOOT_ID_PATH:-/proc/sys/kernel/random/boot_id}}"
baseline_path="${2:-${IRLIGHT_BOOT_ID_BASELINE_PATH:-}}"

unknown() {
  local reason="$1"
  printf 'IRLIGHT_BOOT_GENERATION status=UNKNOWN reason=%s\n' "$reason"
  exit 3
}

read_boot_id() {
  local path="$1"
  local unavailable_reason="$2"
  local invalid_reason="$3"
  local output_name="$4"
  local value=""
  local extra=""

  [[ -f "$path" && -r "$path" ]] || unknown "$unavailable_reason"

  exec 3<"$path" || unknown "$unavailable_reason"
  if ! IFS= read -r value <&3; then
    exec 3<&-
    unknown "$invalid_reason"
  fi
  if IFS= read -r extra <&3; then
    exec 3<&-
    unknown "$invalid_reason"
  fi
  exec 3<&-

  [[ "$value" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]] \
    || unknown "$invalid_reason"

  printf -v "$output_name" '%s' "${value,,}"
}

[[ -n "$baseline_path" ]] || unknown baseline_boot_id_unavailable

current_boot_id=""
baseline_boot_id=""
read_boot_id "$current_path" current_boot_id_unavailable invalid_current_boot_id current_boot_id
read_boot_id "$baseline_path" baseline_boot_id_unavailable invalid_baseline_boot_id baseline_boot_id

if [[ "$current_boot_id" != "$baseline_boot_id" ]]; then
  printf 'IRLIGHT_BOOT_GENERATION status=WARNING reason=boot_generation_changed\n'
  exit 1
fi

printf 'IRLIGHT_BOOT_GENERATION status=OK reason=same_boot\n'
exit 0
