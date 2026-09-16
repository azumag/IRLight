#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/node-admin.sh"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
soak_seconds="${SOAK_SECONDS:-600}"
interval_seconds="${SOAK_INTERVAL_SECONDS:-30}"
samples_jsonl="${SOAK_SAMPLES_JSONL:-}"
media_metrics_file="${SOAK_MEDIA_METRICS_FILE:-}"
allow_unmeasured_media="${SOAK_ALLOW_UNMEASURED_MEDIA:-0}"
if [[ ! "$soak_seconds" =~ ^[1-9][0-9]*$ ]]; then
  echo "SOAK_SECONDS must be a positive integer" >&2
  exit 2
fi
if [[ ! "$interval_seconds" =~ ^[1-9][0-9]*$ ]]; then
  echo "SOAK_INTERVAL_SECONDS must be a positive integer" >&2
  exit 2
fi
if [[ "$allow_unmeasured_media" != "0" && "$allow_unmeasured_media" != "1" ]]; then
  echo "SOAK_ALLOW_UNMEASURED_MEDIA must be 0 or 1" >&2
  exit 2
fi
if [[ -n "$samples_jsonl" ]]; then
  if [[ -n "$media_metrics_file" && "$allow_unmeasured_media" == "1" ]]; then
    echo "SOAK_MEDIA_METRICS_FILE and SOAK_ALLOW_UNMEASURED_MEDIA=1 are mutually exclusive" >&2
    exit 2
  fi
  if [[ -z "$media_metrics_file" && "$allow_unmeasured_media" != "1" ]]; then
    echo "SOAK_SAMPLES_JSONL requires SOAK_MEDIA_METRICS_FILE or SOAK_ALLOW_UNMEASURED_MEDIA=1" >&2
    exit 2
  fi
fi

# A soak run owns a disposable Compose project. Do not allow callers to point
# cleanup at an existing developer/operator project through COMPOSE_PROJECT_NAME
# or an IRLight-specific project override.
soak_project="irlight-poc-soak-$$-$RANDOM"
compose=(docker compose -p "$soak_project" -f "$repo_root/docker-compose.poc.yml")
base_url="${BASE_URL:-http://127.0.0.1:8080}"
hls_url="${HLS_URL:-http://127.0.0.1:8888/output/relay/index.m3u8}"
evidence_pid=""

stop_evidence_collector() {
  if [[ -z "$evidence_pid" ]]; then
    return 0
  fi

  if kill -0 "$evidence_pid" 2>/dev/null; then
    kill -TERM "$evidence_pid" 2>/dev/null || true
  fi
  wait "$evidence_pid" 2>/dev/null || true
  evidence_pid=""
}

cleanup() {
  local status=$?
  local cleanup_status=0
  trap - EXIT

  # The evidence collector reads this project's containers. Stop it before
  # tearing the project down so it cannot race cleanup or inspect a recycled
  # project name after the run exits early.
  stop_evidence_collector

  if ! "${compose[@]}" down --rmi local --volumes --remove-orphans; then
    echo "soak cleanup command failed for $soak_project" >&2
    cleanup_status=1
  fi
  if ! python3 "$repo_root/scripts/verify-soak-cleanup.py" --project "$soak_project"; then
    echo "soak cleanup could not be verified for $soak_project" >&2
    cleanup_status=1
  fi

  if (( status != 0 )); then
    exit "$status"
  fi
  exit "$cleanup_status"
}
trap cleanup EXIT

wait_http() {
  local url="$1"
  local deadline=$((SECONDS + 90))
  until curl -fsS --max-time 5 "$url" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "HTTP endpoint did not become ready: $url" >&2
      return 1
    fi
    sleep 1
  done
}

wait_node() {
  local deadline=$((SECONDS + 90))
  local payload
  while (( SECONDS < deadline )); do
    payload="$(node_admin_curl -fsS --max-time 5 "$base_url/internal/nodes" 2>/dev/null || true)"
    if python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("nodes") else 1)' \
      <<<"$payload" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Node Agent did not register before the soak" >&2
  return 1
}

start_evidence_collector() {
  if [[ -z "$samples_jsonl" ]]; then
    return 0
  fi

  local evidence=(
    python3 "$repo_root/scripts/orchestrate-soak-samples.py"
    --project "$soak_project"
    --compose-file "$repo_root/docker-compose.poc.yml"
    --duration-seconds "$soak_seconds"
    --interval-seconds "$interval_seconds"
    --samples-jsonl "$samples_jsonl"
  )
  if [[ -n "$media_metrics_file" ]]; then
    evidence+=(--media-metrics-file "$media_metrics_file")
  else
    evidence+=(--allow-unmeasured-media)
  fi

  "${evidence[@]}" &
  evidence_pid=$!
}

wait_evidence_collector() {
  if [[ -z "$evidence_pid" ]]; then
    return 0
  fi

  local pid="$evidence_pid"
  if wait "$pid"; then
    evidence_pid=""
    return 0
  fi

  # A signal can interrupt wait while leaving the child alive. Terminate and
  # reap it before returning failure so cleanup never tears down Compose under
  # a collector that still believes the project exists.
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  evidence_pid=""
  echo "soak evidence collection failed" >&2
  return 1
}

# Validate the generated project without tearing down anything that may already
# be running on the fixed PoC ports. `up` will fail cleanly on a port collision.
"${compose[@]}" config >/dev/null
"${compose[@]}" up -d --build
wait_http "$base_url/api/status"
wait_http "$hls_url"
wait_node
start_evidence_collector

deadline=$((SECONDS + soak_seconds))
checks=0
while (( SECONDS < deadline )); do
  curl -fsS --max-time 5 "$base_url/api/status" >/dev/null
  curl -fsS --max-time 5 "$hls_url" >/dev/null
  node_admin_curl -fsS --max-time 5 "$base_url/internal/nodes" |
    python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("nodes") else 1)'

  running_services="$("${compose[@]}" ps --status running --services | wc -l | tr -d ' ')"
  if [[ "$running_services" != "4" ]]; then
    echo "expected 4 running services, got $running_services" >&2
    "${compose[@]}" ps >&2
    exit 1
  fi
  checks=$((checks + 1))

  remaining=$((deadline - SECONDS))
  if (( remaining <= 0 )); then
    break
  fi
  if (( remaining < interval_seconds )); then
    sleep "$remaining"
  else
    sleep "$interval_seconds"
  fi
done

wait_evidence_collector

echo "IRLight compose soak passed: ${soak_seconds}s, ${checks} checks."
if [[ -n "$samples_jsonl" ]]; then
  echo "Soak evidence samples: $samples_jsonl"
fi
