#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export EGRESS_RTMP_SINK_FACTORY=rtmp2sink
exec bash "$repo_root/scripts/smoke-egress-reconnect.sh"
