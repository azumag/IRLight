from __future__ import annotations


SINK_POLL_MILLISECONDS = 250


def heartbeat_report_interval_seconds(status_heartbeat_seconds: float) -> float:
    """Round a finite heartbeat up to the existing nominal sink-poll cadence.

    CONNECTED and subsequent progress both come from the same 250 ms callback.
    A positive heartbeat can only report on the first poll at or after it is
    due. This is a nominal interval, not a bound on scheduler or IPC delays.
    Callers retain responsibility for validating finite timer inputs.
    """

    if status_heartbeat_seconds <= 0:
        return 0.0
    poll_seconds = SINK_POLL_MILLISECONDS / 1000.0
    remainder = status_heartbeat_seconds % poll_seconds
    # Avoid ceil(heartbeat / poll_seconds): the quotient can overflow even
    # when the input is finite. Large binary64 values are already exact
    # multiples of this quarter-second interval, so they need no addition.
    if remainder == 0:
        return status_heartbeat_seconds
    return status_heartbeat_seconds + (poll_seconds - remainder)


def liveness_window_can_be_armed(
    liveness_timeout_seconds: float, status_heartbeat_seconds: float
) -> bool:
    """Require a live evidence channel and a wider-than-nominal report window."""

    return (
        liveness_timeout_seconds > 0
        and status_heartbeat_seconds > 0
        and liveness_timeout_seconds
        > heartbeat_report_interval_seconds(status_heartbeat_seconds)
    )
