# Node Agent ingest fallback clock boundary

The Node Agent normally receives `ingest.observed_at` from `IngestPolicyInspector`, which validates the effective observation clock before any MediaMTX access or bitrate-sampling mutation.

If MediaMTX inspection itself fails, the Agent preserves the existing degraded heartbeat behavior by emitting an `UNKNOWN` ingest observation with reason `MEDIAMTX_API_UNAVAILABLE`. That fallback observation has the same clock contract as the normal producer path: `observed_at` must be a finite, non-negative Unix timestamp. Unix epoch `0.0` remains valid.

An `ingest observation clock is invalid` failure from the inspector is not reclassified as MediaMTX unavailability and does not trigger a second wall-clock sample. It propagates directly to the heartbeat loop so no fallback observation is constructed.

For ordinary MediaMTX failures, the fallback samples the wall clock only after the inspector failure is known. If that sample is negative, NaN, positive or negative infinity, non-numeric, boolean, or cannot be represented as a finite float, the Agent raises `ingest observation clock is invalid` instead of constructing the fallback observation. `heartbeat()` therefore does not send an invalid observation to the Control Plane.

This boundary intentionally does not add a future-skew limit or timestamp-ordering policy. It also does not change the handling of ordinary MediaMTX failures when the local clock is valid.
