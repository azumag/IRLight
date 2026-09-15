#!/usr/bin/env bash
set -uo pipefail

# Run Docker-heavy integration smokes on a single GitHub-hosted runner so they
# share Docker/BuildKit layers. In particular, the Continuity image installs a
# large Ubuntu/GStreamer dependency set; rebuilding it on many fresh runners in
# parallel makes CI unnecessarily dependent on package-mirror throughput.
#
# Keep running after an individual failure so one flaky/failed smoke does not
# hide the results of the remaining scenarios. Each smoke owns its own cleanup.
# Bound every scenario independently so one wedged Docker/curl/ffmpeg operation
# cannot consume the complete 60-minute suite budget and hide later results.
# A recent healthy full suite finished in about 15 minutes, while the isolation
# wrapper already permits a nested 120-second overlap probe, so 180 seconds gives
# that slowest known scenario headroom without making the per-scenario bound lax.
smoke_timeout_seconds=180

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
  scripts/smoke-egress-rtmp2-stop-terminal.sh
  scripts/smoke-egress-rtmp2-dns-tls.sh
  scripts/smoke-egress-rtmp2-publish-conflict.sh
)

# Keep one compact, machine-searchable record per scenario in the workflow log.
# The GitHub step summary mirrors the same records when available. Do not add
# command lines, environment values, container inspection, or logs here because
# media credentials may be present in those surfaces.
results=()
failures=()

write_step_summary() {
  local summary_file="${GITHUB_STEP_SUMMARY:-}"
  local result smoke outcome status duration

  if [[ -z "$summary_file" ]]; then
    return 0
  fi

  {
    echo '### Docker integration smoke suite'
    echo
    echo '| Scenario | Result | Exit | Duration |'
    echo '| --- | --- | ---: | ---: |'
    for result in "${results[@]}"; do
      IFS='|' read -r smoke outcome status duration <<<"$result"
      printf '| `%s` | %s | %s | %ss |\n' "$smoke" "$outcome" "$status" "$duration"
    done
  } >>"$summary_file"
}

emit_failure_context() {
  echo '--- Docker smoke runner diagnostics (secret-safe) ---'
  echo 'Filesystem:'
  df -h / || true
  echo 'Docker storage:'
  docker system df || true
  echo 'Compose projects:'
  docker compose ls --all || true
  echo 'Container state (name/image/status only):'
  docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' || true
}

for smoke in "${smokes[@]}"; do
  started_at=$SECONDS
  echo "::group::$smoke"
  if timeout --signal=TERM --kill-after=10s "${smoke_timeout_seconds}s" bash "$smoke"; then
    duration=$((SECONDS - started_at))
    echo "PASS: $smoke"
    printf 'IRLIGHT_DOCKER_SMOKE_RESULT smoke=%s result=PASS exit=0 duration_seconds=%d\n' \
      "$smoke" "$duration"
    results+=("$smoke|PASS|0|$duration")
  else
    status=$?
    duration=$((SECONDS - started_at))
    if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
      echo "::error title=Docker smoke timed out::$smoke exceeded ${smoke_timeout_seconds}s (status $status)"
    else
      echo "::error title=Docker smoke failed::$smoke exited with status $status"
    fi
    printf 'IRLIGHT_DOCKER_SMOKE_RESULT smoke=%s result=FAIL exit=%d duration_seconds=%d\n' \
      "$smoke" "$status" "$duration"
    results+=("$smoke|FAIL|$status|$duration")
    failures+=("$smoke:$status")
  fi
  echo "::endgroup::"
done

write_step_summary

if ((${#failures[@]} > 0)); then
  printf 'Docker smoke failures (%d):\n' "${#failures[@]}" >&2
  printf '  %s\n' "${failures[@]}" >&2
  emit_failure_context >&2
  exit 1
fi

echo "All Docker smoke scenarios passed."
