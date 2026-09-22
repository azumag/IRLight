# Egress attempt process-isolation contract

Issue #586 prepares a process boundary for the legacy `rtmpsink` teardown hang tracked by #545.

## Why a process boundary is required

Fresh CI evidence captured the egress gateway blocked in `EgressAttempt.run()` while synchronously calling `pipeline.set_state(Gst.State.NULL)`. The production `egress-gateway` service intentionally uses `restart: "no"`, so aborting the whole gateway process is not a safe retry mechanism. Moving the blocking call to a Python thread is also insufficient because an old GStreamer pipeline could remain alive while a replacement attempt starts in the same process.

The intended end state is therefore:

- a long-lived parent gateway owns retry policy and `/state/egress.json`;
- each GStreamer attempt is isolated in a child process;
- the parent starts no replacement attempt until the old child is confirmed reaped;
- a bounded teardown may escalate from natural exit to terminate and finally kill;
- if the child still cannot be reaped, the parent fails closed instead of claiming the old attempt was fenced.

## Stage 1 contract

`apps/egress-gateway/attempt_supervisor.py` is deliberately not wired into production yet. It establishes two contracts that can be tested without GStreamer or Docker:

1. `parse_child_attempt_result()` accepts only a small structured result envelope. Unknown fields are rejected so destination URLs, stream keys, raw plugin errors, or other unbounded text cannot silently become IPC output.
2. `reap_child()` gives a child a bounded natural-exit window, then bounded terminate and kill windows. It returns only after `is_alive()` is false. A child that remains alive after kill raises `AttemptSupervisorError`.

Timeout inputs are finite and non-negative; bool, NaN, infinities, negative values, and conversion failures are rejected before touching the process.

## Not yet enabled

This stage does not:

- spawn a child process;
- change the `rtmpsink` or `rtmp2sink` runtime path;
- change Docker restart policy;
- publish `RECONNECTING` earlier;
- change retry/backoff or the existing 45-second reconnect smoke contract;
- pass destination credentials over argv, environment variables, or the result envelope.

The next stage must add an opt-in child runner with parent-only status writes, secret-safe IPC, explicit stop handling, and deterministic PID reaping before any replacement attempt. Production default enablement remains a separate decision after focused Docker evidence is green.
