from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import egress
from rtmp_sink import destination_url_for_sink, parse_rtmp_sink_factory
from runtime_timer_config import RuntimeTimerConfigError, finite_env_float
from secret_inputs import read_destination_url as _read_secret_destination_url
from secret_inputs import read_input_uri
from stack_watchdog import ConnectedStatusStackWatchdog


LOG = logging.getLogger("irlight.egress.entrypoint")
_FINITE_RUNTIME_TIMERS = (
    ("EGRESS_CONNECT_TIMEOUT_SECONDS", 15.0),
    ("EGRESS_STATUS_HEARTBEAT_SECONDS", 5.0),
    ("EGRESS_CONNECT_STABILITY_SECONDS", 3.0),
    ("EGRESS_OUTPUT_STALL_TIMEOUT_SECONDS", 5.0),
)


def _read_destination_url(path: Path) -> str:
    url = _read_secret_destination_url(path)
    try:
        sink_factory = parse_rtmp_sink_factory(os.getenv("EGRESS_RTMP_SINK_FACTORY"))
        return destination_url_for_sink(
            url,
            sink_factory=sink_factory,
            librtmp_timeout_raw=os.getenv("EGRESS_LIBRTMP_SESSION_TIMEOUT_SECONDS"),
        )
    except ValueError:
        # Never log the URL: it may contain the destination stream key.
        LOG.error("invalid RTMP sink or timeout configuration")
        raise RuntimeError("egress destination URL is invalid") from None


def _validate_runtime_timers() -> None:
    # Validate before EgressGateway or EgressAttempt can turn a non-finite
    # value into a disabled/unbounded timeout. Range semantics remain in the
    # existing runtime classes so every finite value keeps its current meaning.
    for name, default in _FINITE_RUNTIME_TIMERS:
        finite_env_float(name, default)


def _start_stack_watchdog() -> None:
    try:
        heartbeat_seconds = finite_env_float("EGRESS_STATUS_HEARTBEAT_SECONDS", 5.0)
        status_file = Path(os.getenv("EGRESS_STATUS_FILE", "/state/egress.json"))
        ConnectedStatusStackWatchdog(
            status_file,
            heartbeat_seconds=heartbeat_seconds,
            started_at=time.time(),
        ).start()
    except Exception:
        # Diagnostics are best-effort and must never block the media path.
        # Keep this generic: exception text can expose local configuration.
        LOG.warning("egress stale-status stack diagnostics unavailable")


def main() -> int:
    try:
        _validate_runtime_timers()
    except RuntimeTimerConfigError:
        # The parser deliberately omits raw values. Keep startup diagnostics
        # generic as well so environment contents never reach logs.
        LOG.error("invalid finite egress runtime timer configuration")
        return 2
    _start_stack_watchdog()
    # Bind credential-bearing readers before egress.main() constructs the
    # gateway. This keeps the GStreamer-heavy module independent from the
    # testable secret-file boundary while covering the production entrypoint.
    egress._read_input_uri = read_input_uri
    egress.read_destination_url = _read_destination_url
    return egress.main()


if __name__ == "__main__":
    raise SystemExit(main())
