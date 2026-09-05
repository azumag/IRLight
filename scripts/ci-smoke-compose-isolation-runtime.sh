#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/irlight-smoke-isolation-runtime.XXXXXX")"
sentinel_project="irlight-smoke-sentinel-$$-$RANDOM"
sentinel_token="sentinel-$$-$RANDOM"
sentinel_compose_file="$tmp_dir/sentinel.compose.yml"
primary_log="$tmp_dir/primary.log"
secondary_log="$tmp_dir/secondary.log"
primary_pid=""
sentinel_started=0

cat >"$sentinel_compose_file" <<EOF
services:
  sentinel:
    image: busybox:1.36.1
    command:
      - sh
      - -c
      - "printf '%s\\n' '$sentinel_token' > /sentinel/marker; while :; do sleep 60; done"
    volumes:
      - irlight-state:/sentinel
volumes:
  irlight-state:
EOF

sentinel_compose=(docker compose -p "$sentinel_project" -f "$sentinel_compose_file")

show_logs() {
  if [[ -s "$primary_log" ]]; then
    echo "--- primary smoke log ---" >&2
    cat "$primary_log" >&2
  fi
  if [[ -s "$secondary_log" ]]; then
    echo "--- overlapping smoke log ---" >&2
    cat "$secondary_log" >&2
  fi
}

cleanup() {
  status=$?
  trap - EXIT INT TERM
  if [[ -n "$primary_pid" ]] && kill -0 "$primary_pid" 2>/dev/null; then
    kill "$primary_pid" 2>/dev/null || true
    wait "$primary_pid" 2>/dev/null || true
  fi
  if (( sentinel_started )); then
    "${sentinel_compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  if [[ $status -ne 0 ]]; then
    show_logs
  fi
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT INT TERM

"${sentinel_compose[@]}" up -d
sentinel_started=1

sentinel_container_id="$("${sentinel_compose[@]}" ps -q sentinel)"
if [[ -z "$sentinel_container_id" ]]; then
  echo "sentinel container did not start" >&2
  exit 1
fi
mapfile -t sentinel_volumes < <(
  docker volume ls \
    --filter "label=com.docker.compose.project=$sentinel_project" \
    --filter "label=com.docker.compose.volume=irlight-state" \
    --format '{{.Name}}'
)
if ((${#sentinel_volumes[@]} != 1)); then
  echo "expected exactly one sentinel volume, found ${#sentinel_volumes[@]}" >&2
  exit 1
fi
sentinel_volume_name="${sentinel_volumes[0]}"
if [[ "$(docker exec "$sentinel_container_id" cat /sentinel/marker)" != "$sentinel_token" ]]; then
  echo "sentinel volume marker was not initialized" >&2
  exit 1
fi

echo "Starting primary Compose smoke with an unrelated sentinel project running."
bash "$repo_root/scripts/smoke-compose.sh" >"$primary_log" 2>&1 &
primary_pid=$!

ready_deadline=$((SECONDS + 120))
until curl -fsS --max-time 2 http://127.0.0.1:8080/healthz >/dev/null 2>&1; do
  if ! kill -0 "$primary_pid" 2>/dev/null; then
    set +e
    wait "$primary_pid"
    primary_status=$?
    set -e
    primary_pid=""
    echo "primary smoke exited before becoming ready (status $primary_status)" >&2
    exit 1
  fi
  if (( SECONDS >= ready_deadline )); then
    echo "primary smoke did not expose healthz within 120 seconds" >&2
    exit 1
  fi
  sleep 1
done

# A second run must not reclaim the first run's project to solve the fixed-port
# collision. It should fail promptly while the first run remains healthy.
set +e
timeout --signal=TERM 120 bash "$repo_root/scripts/smoke-compose.sh" >"$secondary_log" 2>&1
secondary_status=$?
set -e
if [[ $secondary_status -eq 0 ]]; then
  echo "overlapping smoke unexpectedly succeeded despite fixed host ports" >&2
  exit 1
fi
if [[ $secondary_status -eq 124 ]]; then
  echo "overlapping smoke did not fail promptly on the host-port collision" >&2
  exit 1
fi
if ! grep -Eiq 'address already in use|port is already allocated|failed to bind|bind for .* failed|driver failed programming external connectivity' "$secondary_log"; then
  echo "overlapping smoke failed for an unexpected reason" >&2
  exit 1
fi
if ! curl -fsS --max-time 3 http://127.0.0.1:8080/healthz >/dev/null; then
  echo "primary smoke became unhealthy after overlapping smoke cleanup" >&2
  exit 1
fi

set +e
wait "$primary_pid"
primary_status=$?
set -e
primary_pid=""
if [[ $primary_status -ne 0 ]]; then
  echo "primary smoke failed after the overlapping run (status $primary_status)" >&2
  exit 1
fi

current_sentinel_container_id="$("${sentinel_compose[@]}" ps -q sentinel)"
if [[ "$current_sentinel_container_id" != "$sentinel_container_id" ]]; then
  echo "unrelated sentinel container identity changed during the smoke" >&2
  exit 1
fi
mapfile -t current_sentinel_volumes < <(
  docker volume ls \
    --filter "label=com.docker.compose.project=$sentinel_project" \
    --filter "label=com.docker.compose.volume=irlight-state" \
    --format '{{.Name}}'
)
if ((${#current_sentinel_volumes[@]} != 1)) || [[ "${current_sentinel_volumes[0]}" != "$sentinel_volume_name" ]]; then
  echo "unrelated sentinel volume identity changed during the smoke" >&2
  exit 1
fi
if [[ "$(docker inspect -f '{{.State.Running}}' "$sentinel_container_id")" != "true" ]]; then
  echo "unrelated sentinel container was stopped by the smoke" >&2
  exit 1
fi
if [[ "$(docker exec "$sentinel_container_id" cat /sentinel/marker)" != "$sentinel_token" ]]; then
  echo "unrelated sentinel volume contents changed during the smoke" >&2
  exit 1
fi

echo "Primary smoke passed; overlapping run failed cleanly on fixed ports; unrelated Compose container/volume stayed unchanged."
cat "$primary_log"
echo "--- expected overlapping-run collision (tail) ---"
tail -n 20 "$secondary_log"
