#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
sockstat_path="${1:-/proc/net/sockstat}"
tcp_mem_path="${2:-/proc/sys/net/ipv4/tcp_mem}"

exec python3 "$script_dir/check-host-tcp-memory-pressure.py" \
  --sockstat "$sockstat_path" \
  --tcp-mem "$tcp_mem_path"
