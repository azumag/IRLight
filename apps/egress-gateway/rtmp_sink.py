from __future__ import annotations

from dataclasses import dataclass

from rtmp_session import parse_librtmp_session_timeout, with_librtmp_session_timeout


DEFAULT_RTMP_SINK_FACTORY = "rtmpsink"
RTMP2_SINK_FACTORY = "rtmp2sink"
ALLOWED_RTMP_SINK_FACTORIES = {DEFAULT_RTMP_SINK_FACTORY, RTMP2_SINK_FACTORY}


def parse_rtmp_sink_factory(raw: str | None) -> str:
    """Return the explicitly supported GStreamer RTMP sink factory.

    The migration remains opt-in: an unset value keeps the legacy rtmpsink
    path. Unknown values fail closed rather than allowing an arbitrary plugin
    name to be instantiated from configuration.
    """

    value = DEFAULT_RTMP_SINK_FACTORY if raw is None else raw.strip().lower()
    if not value:
        value = DEFAULT_RTMP_SINK_FACTORY
    if value not in ALLOWED_RTMP_SINK_FACTORIES:
        raise ValueError("unsupported RTMP sink factory")
    return value


def destination_url_for_sink(
    url: str,
    *,
    sink_factory: str,
    librtmp_timeout_raw: str | None,
) -> str:
    """Prepare the credentialed URL for the selected sink without logging it.

    Only legacy rtmpsink/librtmp accepts the whitespace-separated
    ``timeout=<seconds>`` session parameter. rtmp2sink parses the RTMP URI
    itself and has a native timeout property, so its URL must remain byte-for-
    byte unchanged here.
    """

    factory = parse_rtmp_sink_factory(sink_factory)
    if factory == RTMP2_SINK_FACTORY:
        return url
    timeout_seconds = parse_librtmp_session_timeout(librtmp_timeout_raw)
    return with_librtmp_session_timeout(url, timeout_seconds)


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0


@dataclass(frozen=True)
class SinkProgress:
    """Safe progress signals used by the egress liveness state machine.

    ``rendered_buffers`` preserves the existing public status field. For the
    rtmp2 experiment it is the number of FLV buffers observed at the sink pad;
    connection/liveness itself is *not* inferred from that count. It also
    requires transport-byte progress from rtmp2sink.
    """

    rendered_buffers: int
    ready: bool
    progress_marker: tuple[int, int]
    transport_bytes_out: int = 0
    transport_bytes_acked: int = 0


def sink_progress(
    sink_factory: str,
    stats: dict[str, object],
    *,
    observed_sink_buffers: int = 0,
) -> SinkProgress:
    factory = parse_rtmp_sink_factory(sink_factory)
    if factory == DEFAULT_RTMP_SINK_FACTORY:
        rendered = _nonnegative_int(stats.get("rendered"))
        return SinkProgress(
            rendered_buffers=rendered,
            ready=rendered > 0,
            progress_marker=(rendered, 0),
        )

    out_bytes = _nonnegative_int(stats.get("out-bytes-total"))
    acked_bytes = _nonnegative_int(stats.get("out-bytes-acked"))
    observed_buffers = _nonnegative_int(observed_sink_buffers)
    return SinkProgress(
        rendered_buffers=observed_buffers,
        # A buffer reaching the sink pad alone does not prove destination
        # progress. Require the rtmp2 transport to have emitted bytes as well.
        ready=observed_buffers > 0 and out_bytes > 0,
        progress_marker=(out_bytes, acked_bytes),
        transport_bytes_out=out_bytes,
        transport_bytes_acked=acked_bytes,
    )
