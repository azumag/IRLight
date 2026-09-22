# Ingest observation clock validation

`IngestPolicyInspector` uses `observed_at` both as heartbeat evidence and as the time base for aggregate ingest bitrate sampling. That clock is therefore validated before the inspector reads MediaMTX state or mutates sampling history.

The effective observation clock is the explicit `now` test hook when supplied, otherwise `time.time()`. It must be a real integer or float that converts to a finite, non-negative Unix timestamp. Boolean values, strings, negative values, NaN, positive or negative infinity, and values that overflow finite float conversion are rejected with `RuntimeError("ingest observation clock is invalid")`.

Unix epoch `0.0` remains valid. This change deliberately does not add a future-skew limit or a timestamp-ordering policy.

Validation happens before `_path_snapshot()`. When the clock is invalid, the inspector does not contact the MediaMTX Control API, does not consume a fake/test snapshot, does not update bitrate sampling history, and cannot reach the publisher kick endpoint. The caller's existing heartbeat failure handling remains responsible for deciding when to retry the observation.

This keeps the Node Agent producer contract aligned with the Control Plane heartbeat boundary, which already requires `ingest.observed_at` to be finite and non-negative, and with the equivalent fail-closed clock behavior used by relay-client and egress observations.
