# Egress librtmp session timeout

The legacy GStreamer `rtmpsink` used by IRLight delegates RTMP transport to
librtmp. librtmp's session timeout is independent from the Gateway's
`EGRESS_OUTPUT_STALL_TIMEOUT_SECONDS`: the sink can continue accepting buffers
while a dead remote peer has not yet been declared unavailable.

IRLight therefore starts the Egress Gateway through `egress_entrypoint.py`,
which adds librtmp's documented `timeout=<seconds>` session parameter to the
in-memory sink location. The original credentialed destination file is not
rewritten and the resulting URL is never logged or persisted.

`EGRESS_LIBRTMP_SESSION_TIMEOUT_SECONDS` controls the bound:

- default: `30`
- `0` or a negative value: do not add an explicit librtmp timeout
- positive fractional values: round up to whole seconds
- maximum: `300`
- non-finite or non-numeric values: fail closed before connecting

Destination URLs containing whitespace are rejected by this boundary rather
than being allowed to inject arbitrary librtmp session parameters.

This timeout is a transport liveness bound, not a retry delay. After librtmp
reports the outage, the existing Egress Gateway classification and exponential
backoff still decide whether the attempt is retryable and when the next attempt
starts. The existing `scripts/smoke-egress-reconnect.sh` test deliberately
stops the RTMP target and requires `RECONNECTING` within 45 seconds, so the
30-second default keeps that documented recovery contract testable without
weakening the smoke assertion.
