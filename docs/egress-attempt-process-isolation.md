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

`apps/egress-gateway/attempt_supervisor.py` establishes the bounded result and PID-reap contracts:

1. `parse_child_attempt_result()` accepts only a small structured result envelope. Unknown fields are rejected so destination URLs, stream keys, raw plugin errors, or other unbounded text cannot silently become IPC output.
2. `reap_child()` gives a child a bounded natural-exit window, then bounded terminate and kill windows. It returns only after `is_alive()` is false. A child that remains alive after kill raises `AttemptSupervisorError`.

Timeout inputs are finite and non-negative; bool, NaN, infinities, negative values, and conversion failures are rejected before touching the process.

## Stage 2 legacy canary

`apps/egress-gateway/isolated_attempt.py` adds an opt-in parent/child runner. It is enabled only when `EGRESS_LEGACY_PROCESS_ISOLATION_CANARY=1` and the selected sink is legacy `rtmpsink`. `rtmp2sink` remains on the existing in-process path even if the flag is present.

The parent uses the multiprocessing `spawn` context rather than `fork`, because the gateway has already initialized Gst/GLib before attempts begin. Only non-secret configuration is serialized into the child process arguments. The child re-reads the RTSP input and RTMP destination through the existing runtime secret readers, re-applies the librtmp session-timeout rewrite, and re-runs the destination runtime guard before constructing the GStreamer attempt. Destination URLs, stream keys, tokens, and raw plugin errors are not sent through the child result/progress pipe.

The child sends only four bounded message shapes:

- `connected` and `progress`, containing a non-negative rendered-buffer count;
- `teardown`, containing the same bounded `ChildAttemptResult` envelope as Stage 1 before synchronous pipeline teardown;
- `result`, containing the final bounded result after teardown completes.

The parent remains the only writer of `/state/egress.json`: it translates child progress messages back into the existing `on_connected` / `on_progress` callbacks. The child never receives the status-file path as part of its process configuration.

### Teardown and stop fencing

A small child-side monitor observes the GLib main-loop transition into teardown and emits the sanitized result snapshot before `EgressAttempt.run()` reaches the known blocking `set_state(NULL)` call. After that marker, the parent allows a bounded teardown window and then calls the Stage 1 reaper. No replacement attempt can start until the old child is confirmed dead.

The default canary windows are:

- `EGRESS_ISOLATED_TEARDOWN_TIMEOUT_SECONDS=8`
- `EGRESS_ISOLATED_TERMINATE_TIMEOUT_SECONDS=2`
- `EGRESS_ISOLATED_KILL_TIMEOUT_SECONDS=2`

They are only read when the canary is enabled, must be finite and non-negative, and do not alter the existing retry/backoff configuration. Before the first `CONNECTED` event, the supervisor also bounds an otherwise stuck attempt to the existing connect timeout plus the teardown window. A configured connect timeout of `0` keeps the historical disabled-timeout behavior.

User stop sets a child-shared stop event first, then applies the same bounded teardown/reap sequence. IPC flooding cannot extend the stop or teardown deadline because deadlines are evaluated after every received message.

### Failure mapping

- malformed/unknown IPC -> `LOCAL_PIPELINE_FAILED`, terminal;
- child crash without a valid final result -> `LOCAL_PIPELINE_FAILED`, terminal;
- destination runtime guard rejection -> its existing non-secret reason/terminal classification;
- bounded pre-connect timeout -> `TIMEOUT`, retryable;
- user stop after successful child reap -> `STOPPED`;
- a child that remains alive even after kill -> `AttemptSupervisorError` in the parent, rather than starting an overlapping attempt.

Terminal `AUTH_FAILED` / `PUBLISH_CONFLICT` results remain terminal because their sanitized result is preserved across teardown fencing.

## Still not enabled by default

The canary does not:

- change the production default from the in-process legacy path;
- apply process isolation to `rtmp2sink`;
- change Docker restart policy;
- publish `RECONNECTING` before the old child is fenced;
- change retry/backoff policy or extend the existing 45-second reconnect contract;
- use an external RTMP/RTMPS destination for validation.

The next required evidence is a focused Docker canary smoke that forces the legacy teardown-hang condition, proves the old child PID is reaped before retry, verifies `RECONNECTING` within 45 seconds and subsequent `CONNECTED` recovery, and covers stop-terminal/auth-terminal plus the unchanged `rtmp2sink` path before any production-default decision.
