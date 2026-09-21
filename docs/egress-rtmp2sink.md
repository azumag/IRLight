# Experimental `rtmp2sink` egress path

IRLight still defaults to GStreamer's legacy `rtmpsink` / librtmp transport.
Issue #131 tracks migration to `rtmp2sink`. The migration is deliberately
opt-in while its platform compatibility and failure classification are being
measured.

Set `EGRESS_RTMP_SINK_FACTORY=rtmp2sink` on the Egress Gateway to select the
new transport. The only accepted values are `rtmpsink` and `rtmp2sink`; an
unknown plugin name fails closed as `LOCAL_PIPELINE_FAILED`. The default stays
`rtmpsink`, so this change does not silently alter an existing deployment.

## Liveness signals

The legacy path keeps using `GstBaseSink`'s rendered-buffer statistic. The
`rtmp2sink` path does not treat an FLV buffer reaching the sink pad as proof of
network health. It requires both media at the sink pad and transport progress
from `rtmp2sink`'s connection statistics. `out-bytes-total` drives transport
progress and `out-bytes-acked` is tracked as an additional progress signal, so
an ACK change is visible to the same stall detector. GStreamer bus errors
remain authoritative and terminate/retry an attempt according to the existing
stable reason-code policy.

Sink counters are accepted as liveness evidence only when GStreamer exposes
them as actual non-boolean integer values. Missing values, negative values,
bools, numeric strings, floats, and other malformed values fail closed to zero
instead of being coerced into apparent progress. This applies to the legacy
`rendered` counter as well as the `rtmp2sink` transport counters.

`rendered_buffers` remains in the public status schema for compatibility. On
this experimental path it counts FLV buffers observed at the sink pad; it is a
diagnostic counter, not the connection predicate.

## Secrets and URL handling

`rtmp2sink` receives the credentialed RTMP/RTMPS URL only through the existing
secret-file path. The legacy whitespace `timeout=<seconds>` librtmp session
parameter is **not** appended when `rtmp2sink` is selected. Raw sink errors and
destination URLs are still excluded from status and logs, and the existing
runtime destination/DNS guard runs before either sink is constructed.

## TLS

`rtmp2sink` supports both `rtmp` and `rtmps`. Its `tls-validation-flags`
default is `validate-all`; IRLight does not override or weaken it. The Docker
migration smoke includes a self-signed RTMPS target and requires the attempt
to fail with `TLS_FAILED` without leaking the stream key.

## CI migration probes

The shared Docker smoke suite runs the existing legacy scenarios unchanged and
also runs four opt-in `rtmp2sink` variants:

- local RTMP publish, remote target stop, reconnect, and recovery;
- explicit Gateway stop while reconnect backoff is pending, followed by a
  terminal unsafe-destination rejection without reconnect or secret leakage;
- DNS failure plus self-signed RTMPS certificate rejection;
- publish-conflict classification and secret non-disclosure.

The explicit-stop scenario is intentionally shared with the legacy smoke. It
proves that selecting `rtmp2sink` does not weaken the existing user-stop
precedence or terminal-failure contract while migration evidence is collected.

`tests/test_rtmp2_smoke_wiring.py` protects the migration harness itself. It
requires every `rtmp2sink` wrapper to force `EGRESS_RTMP_SINK_FACTORY=rtmp2sink`,
requires the shared scenario's temporary Compose definition to forward that
selection into the Egress Gateway container, and requires the shared Docker
suite to keep both the legacy gate and the matching `rtmp2sink` probe. This
prevents a wrapper/configuration regression from producing a green migration
result while accidentally exercising the legacy sink.

These probes intentionally use only local synthetic credentials. Passing them
is evidence for the migration, not authorization to flip the production
default. Twitch, YouTube, Kick, and Custom destination behavior still needs
real-platform verification before the default can change.
