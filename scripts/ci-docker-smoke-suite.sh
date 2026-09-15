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
# media credentials may be present in those surfaces. A failing smoke may expose
# an existing GitHub annotation with a `stage=` token; copy only that allowlisted
# token into the compact result rather than propagating the rest of diagnostics.
results=()
failures=()
failure_contexts=()
diagnostic_tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/irlight-docker-smoke-suite.XXXXXX")"
trap 'rm -rf "$diagnostic_tmp_dir"' EXIT

extract_failure_stage() {
  local log_file="$1"
  local stage

  stage="$(
    sed -nE \
      's/.*::error title=IRLight docker smoke failure::stage=([A-Za-z0-9._-]+).*/\1/p' \
      "$log_file" | tail -n 1
  )"
  if [[ -z "$stage" ]]; then
    stage="-"
  fi
  printf '%s' "$stage"
}

capture_runner_resources() {
  local output_file="$1"

  # Capture only aggregate runner capacity. These commands do not expose
  # container environment, commands, labels, mounts or logs, so the baseline
  # is safe to retain alongside a failure and can be compared with the
  # failure-boundary snapshot when runner disk/cache pressure is suspected.
  # Keep diagnostics independently bounded so an unhealthy Docker daemon cannot
  # wedge the suite outside the per-scenario timeout.
  {
    echo 'Filesystem:'
    timeout --signal=TERM --kill-after=2s 10s df -h / || true
    echo 'Docker storage:'
    timeout --signal=TERM --kill-after=2s 10s docker system df || true
  } >"$output_file" 2>&1
}

write_step_summary() {
  local summary_file="${GITHUB_STEP_SUMMARY:-}"
  local result smoke outcome status duration stage context context_file
  local resource_before_file resource_after_file
  local project service container image state docker_status

  if [[ -z "$summary_file" ]]; then
    return 0
  fi

  {
    echo '### Docker integration smoke suite'
    echo
    echo '| Scenario | Result | Exit | Duration | Stage |'
    echo '| --- | --- | ---: | ---: | --- |'
    for result in "${results[@]}"; do
      IFS='|' read -r smoke outcome status duration stage <<<"$result"
      printf '| `%s` | %s | %s | %ss | `%s` |\n' \
        "$smoke" "$outcome" "$status" "$duration" "$stage"
    done

    for context in "${failure_contexts[@]}"; do
      IFS='|' read -r smoke context_file resource_before_file resource_after_file <<<"$context"
      echo
      printf '#### Failure context: `%s`\n\n' "$smoke"

      echo '<details><summary>Runner resources before scenario</summary>'
      echo
      echo '```text'
      cat "$resource_before_file" 2>/dev/null || echo '(resource baseline unavailable)'
      echo '```'
      echo '</details>'
      echo
      echo '<details><summary>Runner resources at failure boundary</summary>'
      echo
      echo '```text'
      cat "$resource_after_file" 2>/dev/null || echo '(failure resource snapshot unavailable)'
      echo '```'
      echo '</details>'
      echo

      if [[ ! -s "$context_file" ]]; then
        echo 'No Compose-managed containers remained at the failure boundary.'
        continue
      fi

      echo '| Compose project | Service | Container | Image | State | Status |'
      echo '| --- | --- | --- | --- | --- | --- |'
      while IFS='|' read -r project service container image state docker_status; do
        printf '| `%s` | `%s` | `%s` | `%s` | `%s` | %s |\n' \
          "$project" "$service" "$container" "$image" "$state" "$docker_status"
      done <"$context_file"
    done
  } >>"$summary_file"
}

emit_failure_context() {
  local smoke="$1"
  local resource_before_file="$2"
  local safe_name="${smoke//\//_}"
  local container_state_file resource_after_file

  safe_name="${safe_name//./_}"
  container_state_file="$diagnostic_tmp_dir/${safe_name}.containers"
  resource_after_file="$diagnostic_tmp_dir/${safe_name}.resources-after"
  capture_runner_resources "$resource_after_file"

  # Capture this immediately after the failed scenario returns. Waiting until
  # all 17 smokes finish lets later scenario cleanup erase the very container
  # state needed to distinguish an application failure from runner pressure.
  # Keep the snapshot deliberately narrow: Compose project/service, names,
  # images and Docker state/status are useful for exit/health classification
  # without exposing env, commands, inspect payloads or arbitrary logs.
  printf '%s\n' "--- Docker smoke runner diagnostics (secret-safe; scenario=$smoke) ---"
  echo 'Runner resources before scenario:'
  cat "$resource_before_file" 2>/dev/null || echo '(resource baseline unavailable)'
  echo 'Runner resources at failure boundary:'
  cat "$resource_after_file" 2>/dev/null || echo '(failure resource snapshot unavailable)'
  echo 'Compose projects:'
  docker compose ls --all || true

  if ! docker ps -a \
    --filter label=com.docker.compose.project \
    --format '{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.service"}}|{{.Names}}|{{.Image}}|{{.State}}|{{.Status}}' \
    >"$container_state_file"
  then
    : >"$container_state_file"
  fi

  echo 'Compose container state (project/service/name/image/state/status only):'
  if [[ -s "$container_state_file" ]]; then
    cat "$container_state_file"
  else
    echo '(none)'
  fi
  failure_contexts+=("$smoke|$container_state_file|$resource_before_file|$resource_after_file")
}

for smoke in "${smokes[@]}"; do
  safe_name="${smoke//\//_}"
  safe_name="${safe_name//./_}"
  scenario_log="$diagnostic_tmp_dir/${safe_name}.log"
  resource_before_file="$diagnostic_tmp_dir/${safe_name}.resources-before"
  capture_runner_resources "$resource_before_file"
  started_at=$SECONDS
  echo "::group::$smoke"

  # Keep the scenario output live while retaining a run-local copy from which
  # only a narrowly allowlisted stage token is extracted. PIPESTATUS preserves
  # the smoke exit code instead of treating a successful tee as a passing smoke.
  timeout --signal=TERM --kill-after=10s "${smoke_timeout_seconds}s" bash "$smoke" 2>&1 | tee "$scenario_log"
  status=${PIPESTATUS[0]}
  duration=$((SECONDS - started_at))

  if [[ "$status" -eq 0 ]]; then
    echo "PASS: $smoke"
    printf 'IRLIGHT_DOCKER_SMOKE_RESULT smoke=%s result=PASS exit=0 duration_seconds=%d stage=-\n' \
      "$smoke" "$duration"
    results+=("$smoke|PASS|0|$duration|-")
  else
    stage="$(extract_failure_stage "$scenario_log")"
    if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
      echo "::error title=Docker smoke timed out::$smoke exceeded ${smoke_timeout_seconds}s (status $status)"
    else
      echo "::error title=Docker smoke failed::$smoke exited with status $status"
    fi
    printf 'IRLIGHT_DOCKER_SMOKE_RESULT smoke=%s result=FAIL exit=%d duration_seconds=%d stage=%s\n' \
      "$smoke" "$status" "$duration" "$stage"
    results+=("$smoke|FAIL|$status|$duration|$stage")
    failures+=("$smoke:$status:$stage")
    emit_failure_context "$smoke" "$resource_before_file" >&2
  fi
  echo "::endgroup::"
done

write_step_summary

if ((${#failures[@]} > 0)); then
  printf 'Docker smoke failures (%d):\n' "${#failures[@]}" >&2
  printf '  %s\n' "${failures[@]}" >&2
  exit 1
fi

echo "All Docker smoke scenarios passed."
