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

- default: `20`
- `0` or a negative value: do not add an explicit librtmp timeout
- positive fractional values: round up to whole seconds
- maximum: `300`
- non-finite or non-numeric values: fail closed before connecting

Destination URLs containing whitespace are rejected by this boundary rather
than being allowed to inject arbitrary librtmp session parameters.

This timeout is a transport liveness bound, not a retry delay. After librtmp
reports the outage, the existing Egress Gateway classification and exponential
backoff still decide whether the attempt is retryable and when the next attempt
starts.

The legacy reconnect smokes require `RECONNECTING` within 45 seconds after the
remote target is stopped. A 30-second default was originally chosen to fit that
window, but repeated CI runs showed that the transport timeout plus scheduling,
pipeline teardown, and status persistence could consume the remaining margin
intermittently. The default is therefore 20 seconds so the production legacy
path has explicit margin inside the existing 45-second contract. The smoke
timeout is intentionally not increased, and deployments that need a different
transport bound can still set `EGRESS_LIBRTMP_SESSION_TIMEOUT_SECONDS`
explicitly.

This remains a compatibility bound for the legacy `rtmpsink` path. The opt-in
`rtmp2sink` path does not receive the librtmp session parameter and is tracked
separately for migration/real-platform verification.
