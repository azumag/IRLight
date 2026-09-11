# Egress status inspection clock validation

The Node Agent reads `/state/egress.json` and compares its `observed_at` timestamp with the local inspection clock before forwarding an Egress observation to the Control Plane. That local clock is part of the fail-closed freshness boundary: if it is not trustworthy, the Agent must not report a stale `CONNECTED` observation as current.

`read_egress_status()` therefore validates the effective inspection clock before reading the status file. The explicit `now` test hook and the default `time.time()` path use the same rules:

- booleans and non-numeric values are rejected;
- negative values are rejected;
- `NaN`, positive/negative infinity, and values that overflow float conversion are rejected;
- invalid clocks raise the fixed `RuntimeError("egress status clock is invalid")` before any status-file read.

In the normal Node Agent heartbeat loop, this `RuntimeError` is handled by the existing heartbeat failure path. No Egress observation is sent for that iteration, so the Control Plane can age the Node heartbeat instead of accepting an observation whose freshness cannot be established.

Unknown states caused by a missing or invalid status file reuse the already validated inspection clock for their `observed_at` field. The reader does not call `time.time()` a second time while constructing those fallback observations.

This change does not alter Egress Gateway retry policy, terminal status semantics, destination credential handling, DNS guards, or the configured status max-age threshold.
