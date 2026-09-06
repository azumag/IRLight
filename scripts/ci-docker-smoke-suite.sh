#!/usr/bin/env bash
set -uo pipefail

# Run Docker-heavy integration smokes on a single GitHub-hosted runner so they
# share Docker/BuildKit layers. In particular, the Continuity image installs a
# large Ubuntu/GStreamer dependency set; rebuilding it on many fresh runners in
# parallel makes CI unnecessarily dependent on package-mirror throughput.
#
# Keep running after an individual failure so one flaky/failed smoke does not
# hide the results of the remaining scenarios. Each smoke owns its own cleanup.
smokes=(
  # The wrapper runs smoke-compose.sh while an unrelated sentinel project is
  # alive, then overlaps a second run to prove a fixed-port collision cannot
  # reclaim or tear down the first run or the sentinel project.
  scripts/ci-smoke-compose-isolation-runtime.sh
  scripts/smoke-ingest-quality.sh
  scripts/smoke-continuity-restart.sh
  scripts/smoke-rtmps.sh
  scripts/smoke-unusable-media-holding.sh
  scripts/smoke-ingest-auth-abuse.sh
  scripts/smoke-ingest-auth-cache.sh
  scripts/smoke-session-ingest-events.sh
  scripts/smoke-egress-secret-delivery.sh
  scripts/smoke-egress-reconnect.sh
  scripts/smoke-egress-stop-terminal.sh
  scripts/smoke-egress-dns-tls.sh
  scripts/smoke-egress-publish-conflict.sh
  # Keep rtmp2sink opt-in until these migration probes remain green without
  # weakening the legacy compatibility gates.
  scripts/smoke-egress-rtmp2-reconnect.sh
  scripts/smoke-egress-rtmp2-dns-tls.sh
  scripts/smoke-egress-rtmp2-publish-conflict.sh
)

failures=()
for smoke in "${smokes[@]}"; do
  echo "::group::$smoke"
  if bash "$smoke"; then
    echo "PASS: $smoke"
  else
    status=$?
    echo "::error title=Docker smoke failed::$smoke exited with status $status"
    failures+=("$smoke:$status")
  fi
  echo "::endgroup::"
done

if ((${#failures[@]} > 0)); then
  printf 'Docker smoke failures (%d):\n' "${#failures[@]}" >&2
  printf '  %s\n' "${failures[@]}" >&2
  exit 1
fi

echo "All Docker smoke scenarios passed."
