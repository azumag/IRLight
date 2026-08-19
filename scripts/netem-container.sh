#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  netem-container.sh apply <container> [options]
  netem-container.sh clear <container>
  netem-container.sh show <container>

Options for apply:
  --loss <percent>       Packet loss percentage (0-100), e.g. 5
  --delay-ms <ms>        Added latency in milliseconds, e.g. 100
  --jitter-ms <ms>       Delay jitter in milliseconds; requires --delay-ms > 0
  --rate <rate>          Netem rate, e.g. 4mbit, 800kbit
  --interface <name>     Interface inside the container namespace (default: eth0)

Examples:
  ./scripts/netem-container.sh apply publisher --loss 5
  ./scripts/netem-container.sh apply publisher --delay-ms 100 --jitter-ms 20 --rate 4mbit
  ./scripts/netem-container.sh show publisher
  ./scripts/netem-container.sh clear publisher

This helper is intended for Linux Docker hosts. It enters the running
container's network namespace with nsenter and applies tc netem from the host,
so tc/iproute2 does not need to be installed inside the target container.
EOF
}

fail() {
  echo "netem-container: $*" >&2
  exit 2
}

run_root() {
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    "$@"
  else
    command -v sudo >/dev/null 2>&1 || fail "sudo is required when not running as root"
    sudo "$@"
  fi
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

validate_number() {
  local name="$1" value="$2"
  [[ "$value" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "$name must be a non-negative number: $value"
}

[[ $# -ge 2 ]] || { usage >&2; exit 2; }
ACTION="$1"
CONTAINER="$2"
shift 2

[[ "$(uname -s)" == "Linux" ]] || fail "Linux Docker host required"
require_command docker
require_command nsenter
require_command tc

PID="$(docker inspect -f '{{.State.Pid}}' "$CONTAINER" 2>/dev/null || true)"
[[ "$PID" =~ ^[1-9][0-9]*$ ]] || fail "container is not running or not found: $CONTAINER"

INTERFACE="${NETEM_INTERFACE:-eth0}"

netns_tc() {
  run_root nsenter -t "$PID" -n -- tc "$@"
}

case "$ACTION" in
  show)
    [[ $# -eq 0 ]] || fail "show does not accept additional arguments"
    netns_tc qdisc show dev "$INTERFACE"
    ;;

  clear)
    [[ $# -eq 0 ]] || fail "clear does not accept additional arguments"
    # qdisc delete returns a non-zero status when no root qdisc is installed.
    # Treat that case as already clear so cleanup remains idempotent.
    if ! netns_tc qdisc del dev "$INTERFACE" root 2>/dev/null; then
      :
    fi
    ;;

  apply)
    LOSS=""
    DELAY_MS=""
    JITTER_MS=""
    RATE=""

    while [[ $# -gt 0 ]]; do
      case "$1" in
        --loss)
          [[ $# -ge 2 ]] || fail "--loss requires a value"
          LOSS="$2"
          shift 2
          ;;
        --delay-ms)
          [[ $# -ge 2 ]] || fail "--delay-ms requires a value"
          DELAY_MS="$2"
          shift 2
          ;;
        --jitter-ms)
          [[ $# -ge 2 ]] || fail "--jitter-ms requires a value"
          JITTER_MS="$2"
          shift 2
          ;;
        --rate)
          [[ $# -ge 2 ]] || fail "--rate requires a value"
          RATE="$2"
          shift 2
          ;;
        --interface)
          [[ $# -ge 2 ]] || fail "--interface requires a value"
          INTERFACE="$2"
          shift 2
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        *)
          fail "unknown option: $1"
          ;;
      esac
    done

    if [[ -n "$LOSS" ]]; then
      validate_number "loss" "$LOSS"
      python3 - "$LOSS" <<'PY'
import sys
v = float(sys.argv[1])
if not 0 <= v <= 100:
    raise SystemExit("loss must be between 0 and 100")
PY
    fi
    if [[ -n "$DELAY_MS" ]]; then
      validate_number "delay-ms" "$DELAY_MS"
    fi
    if [[ -n "$JITTER_MS" ]]; then
      validate_number "jitter-ms" "$JITTER_MS"
      [[ -n "$DELAY_MS" ]] || fail "--jitter-ms requires --delay-ms"
      python3 - "$DELAY_MS" <<'PY'
import sys
if float(sys.argv[1]) <= 0:
    raise SystemExit("--jitter-ms requires --delay-ms > 0")
PY
    fi
    if [[ -n "$RATE" ]]; then
      [[ "$RATE" =~ ^[0-9]+([.][0-9]+)?(kbit|mbit|gbit)$ ]] || fail "rate must look like 800kbit, 4mbit, or 1gbit: $RATE"
    fi

    [[ -n "$LOSS" || -n "$DELAY_MS" || -n "$RATE" ]] || fail "apply requires at least one impairment"

    ARGS=(qdisc replace dev "$INTERFACE" root netem)
    if [[ -n "$DELAY_MS" ]]; then
      ARGS+=(delay "${DELAY_MS}ms")
      if [[ -n "$JITTER_MS" ]]; then
        ARGS+=("${JITTER_MS}ms")
      fi
    fi
    if [[ -n "$LOSS" ]]; then
      ARGS+=(loss "${LOSS}%")
    fi
    if [[ -n "$RATE" ]]; then
      ARGS+=(rate "$RATE")
    fi

    netns_tc "${ARGS[@]}"
    ;;

  *)
    usage >&2
    fail "unknown action: $ACTION"
    ;;
esac
