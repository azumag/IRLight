#!/usr/bin/env bash
set -euo pipefail

tmp_dir="$(mktemp -d)"
override="$tmp_dir/hold-timeout-cleanup.override.yml"
cookie_jar="$tmp_dir/cookies.txt"
base_url="${BASE_URL:-http://127.0.0.1:8080}"
email="hold-timeout-$(date +%s)-$RANDOM@example.invalid"
password='SmokePassword123!'
hold_timeout_seconds="${HOLD_TIMEOUT_SECONDS:-2}"

cat >"$override" <<YAML
services:
  control-ui:
    environment:
      IRLIGHT_PROVIDER: fake
      IRLIGHT_FAKE_PROVIDER_STATE_FILE: /state/fake-provider.json
      COOKIE_INSECURE: "1"
YAML

compose=(docker compose -f docker-compose.poc.yml -f "$override")

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- compose ps ---" >&2
    "${compose[@]}" ps -a >&2 || true
    echo "--- control logs ---" >&2
    "${compose[@]}" logs --no-color --tail=180 control-ui >&2 || true
    echo "--- sessions state ---" >&2
    "${compose[@]}" exec -T continuity sh -c 'cat /state/sessions.json 2>/dev/null || true' >&2 || true
    echo "--- fake provider state ---" >&2
    "${compose[@]}" exec -T continuity sh -c 'cat /state/fake-provider.json 2>/dev/null || true' >&2 || true
  fi
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT

wait_http() {
  local url="$1"
  local timeout="${2:-60}"
  local deadline=$((SECONDS + timeout))
  until curl -fsS --max-time 3 "$url" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "HTTP endpoint did not become ready: $url" >&2
      return 1
    fi
    sleep 1
  done
}

login() {
  local response
  rm -f "$cookie_jar"
  response="$(curl -fsS --max-time 10 -c "$cookie_jar" -X POST "$base_url/v1/auth/login" \
    -H 'Content-Type: application/json' \
    --data "{\"email\":\"$email\",\"password\":\"$password\"}")"
  csrf="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])' <<<"$response")"
}

read_provider_state() {
  "${compose[@]}" exec -T continuity cat /state/fake-provider.json 2>/dev/null
}

read_session_state() {
  "${compose[@]}" run --rm --no-deps control-ui \
    python -c 'import json,sys; from fake_provider_for_api import default_store; value=default_store().get(sys.argv[1]); print(json.dumps(value, sort_keys=True))' \
    "$session_id"
}

"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" up -d --build control-ui
wait_http "$base_url/healthz" 60

curl -fsS --max-time 10 -X POST "$base_url/v1/auth/register" \
  -H 'Content-Type: application/json' \
  --data "{\"email\":\"$email\",\"password\":\"$password\",\"display_name\":\"Hold Timeout Smoke\"}" >/dev/null
login

session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
prepared="$(curl -fsS --max-time 10 -b "$cookie_jar" -X POST \
  "$base_url/v1/sessions/$session_id/prepare" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $csrf" \
  -H "Idempotency-Key: hold-timeout-$session_id" \
  --data '{"environment":"dev"}')"
provider_server_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["provider_server_id"])' <<<"$prepared")"
provider_volume_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["provider_volume_id"])' <<<"$prepared")"

provider_before="$(read_provider_state)"
python3 -c '
import json,sys
state=json.loads(sys.argv[1])
server_id=sys.argv[2]
volume_id=sys.argv[3]
assert any(item.get("server_id") == server_id for item in state.get("servers", [])), state
assert any(item.get("volume_id") == volume_id for item in state.get("volumes", [])), state
' "$provider_before" "$provider_server_id" "$provider_volume_id"

# Stop the API process before mutating the same persisted SessionStore from a
# one-off container. This keeps the test focused on Reaper persistence and
# avoids two independent SessionStore process caches writing concurrently.
"${compose[@]}" stop control-ui >/dev/null

# PR #59 already covers the real publisher path into HOLDING. Here we seed the
# persisted lifecycle at HOLDING so this E2E can isolate maximum-hold expiry and
# provider cleanup without repeating the media stack setup.
"${compose[@]}" run --rm --no-deps control-ui python - "$session_id" "$hold_timeout_seconds" <<'PY'
from __future__ import annotations

import sys
import time

from fake_provider_for_api import default_store

session_id = sys.argv[1]
hold_timeout = float(sys.argv[2])
store = default_store()
session = store.get(session_id)
if session is None:
    raise SystemExit(f"missing session: {session_id}")
if session.get("status") != "READY_WAIT_INGEST":
    raise SystemExit(f"unexpected prepared state: {session.get('status')}")

now = time.time()
store.transition(
    session_id,
    "LIVE",
    allow_from={"READY_WAIT_INGEST"},
    first_ingest_at=now - hold_timeout - 2.0,
)
store.transition(
    session_id,
    "HOLDING",
    allow_from={"LIVE"},
    last_ingest_at=now - hold_timeout - 1.0,
    hold_deadline_at=None,
)
store.append_event(
    session_id,
    event_type="session.holding",
    reason_code="INGEST_DISCONNECTED",
    payload={"from_state": "LIVE", "to_state": "HOLDING"},
    origin="qa-smoke",
    occurred_at=now - hold_timeout - 1.0,
)
PY

holding_state="$(read_session_state)"
python3 -c '
import json,sys
session=json.loads(sys.argv[1])
assert session.get("status") == "HOLDING", session
assert session.get("hold_deadline_at") is None, session
assert any(
    event.get("type") == "session.holding" and event.get("reason_code") == "INGEST_DISCONNECTED"
    for event in session.get("events", [])
), session
' "$holding_state"

reaper_output="$("${compose[@]}" run --rm --no-deps control-ui \
  python reaper_cli.py \
  --provisioning-timeout-seconds 3600 \
  --no-ingest-timeout-seconds 3600 \
  --hold-timeout-seconds "$hold_timeout_seconds" \
  --heartbeat-grace-seconds 3600)"
echo "$reaper_output"
python3 -c '
import ast,sys
result=ast.literal_eval(sys.argv[1].strip().splitlines()[-1])
assert result.get("hold_deadlines_recovered") == 1, result
assert result.get("deadline_stops") == 1, result
' "$reaper_output"

finished_state="$(read_session_state)"
python3 -c '
import json,sys
session=json.loads(sys.argv[1])
assert session.get("status") == "FINISHED", session
assert session.get("cleanup_pending") is False, session
stopping=[e for e in session.get("events", []) if e.get("type") == "session.stopping"]
finished=[e for e in session.get("events", []) if e.get("type") == "session.finished"]
assert stopping and stopping[-1].get("reason_code") == "HOLD_TIMEOUT", session
assert finished and finished[-1].get("reason_code") == "HOLD_TIMEOUT", session
' "$finished_state"

provider_after="$(read_provider_state)"
python3 -c '
import json,sys
state=json.loads(sys.argv[1])
server_id=sys.argv[2]
volume_id=sys.argv[3]
assert state.get("servers") == [], state
assert state.get("volumes") == [], state
assert server_id not in sys.argv[1], state
assert volume_id not in sys.argv[1], state
' "$provider_after" "$provider_server_id" "$provider_volume_id"

# A second sweep must be idempotent: no new deadline stop and no resurrected
# provider resources or Session state changes.
second_reaper_output="$("${compose[@]}" run --rm --no-deps control-ui \
  python reaper_cli.py \
  --provisioning-timeout-seconds 3600 \
  --no-ingest-timeout-seconds 3600 \
  --hold-timeout-seconds "$hold_timeout_seconds" \
  --heartbeat-grace-seconds 3600)"
python3 -c '
import ast,sys
result=ast.literal_eval(sys.argv[1].strip().splitlines()[-1])
assert result.get("deadline_stops") == 0, result
assert result.get("failed_cleanup_retries") == 0, result
' "$second_reaper_output"

finished_again="$(read_session_state)"
python3 -c '
import json,sys
session=json.loads(sys.argv[1])
assert session.get("status") == "FINISHED", session
assert session.get("cleanup_pending") is False, session
' "$finished_again"

echo "HOLD_TIMEOUT cleanup smoke passed: FINISHED and provider inventory empty"
