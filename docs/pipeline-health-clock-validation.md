# Pipeline health clock validation

The Control Plane's media-pipeline health policy uses an observation timestamp to decide when a transient media failure has lasted long enough to become `PIPELINE_CRASHED`. That timestamp is therefore part of the fail-closed lifecycle boundary: an invalid clock must not make the grace comparison silently succeed or fail.

`apply_pipeline_health()` now normalizes its effective observation time before it mutates the Node health fields or reads/mutates the Session. The normalizer accepts only non-negative finite numeric values. Booleans, negative values, `NaN`, positive or negative infinity, and integers that overflow float normalization are rejected with a fixed `ValueError` message. The same check applies whether a caller supplies `observed_at` explicitly or the helper uses `time.time()`.

A rejected observation does not start/reset `media_unhealthy_since`, set `media_failure_reason`, change `desired_state`, or perform a Session lookup/transition. Existing grace-period, explicit `FAILED` bypass, cleanup, provider, billing, and notification semantics are unchanged.

This is intentionally a narrow runtime-clock guard. It does not change the configured media-health grace duration or define new health/status vocabulary. Broader Node heartbeat clock consistency remains part of the persisted-authority audit tracked in #87.

Refs #87
