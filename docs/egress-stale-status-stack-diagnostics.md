# Egress stale CONNECTED stack diagnostics

Issue #545 tracks an intermittent legacy `rtmpsink` case where a stopped RTMP target is not reflected as `RECONNECTING` within the existing 45-second smoke contract. The reconnect smoke already records secret-safe status age and rendered-buffer evidence. This diagnostic adds one more boundary without changing egress behavior: when a current-process `CONNECTED` status stops refreshing, the process emits a Python thread stack snapshot.

## Trigger

The watchdog is enabled only when `EGRESS_STATUS_HEARTBEAT_SECONDS` is greater than zero. A snapshot is eligible when all of the following are true:

- the status is `CONNECTED` with `connected=true`;
- `observed_at` belongs to the current process generation rather than a record left by an earlier process;
- the status age reaches `max(15 seconds, 3 x heartbeat interval)`;
- the exact stale `observed_at` value has not already produced a dump.

A refreshed status can later become stale and produce a new snapshot. Setting the heartbeat interval to zero disables the watchdog because status age is no longer a valid indication of a blocked attempt.

## Secret boundary

The watchdog does not print the status JSON, destination URL, destination host, stream key, plugin error text, or Python local variables. It emits only the fixed marker

```text
IRLIGHT_EGRESS_STALE_STATUS_STACK status=CONNECTED age_bucket=stale
```

followed by `faulthandler` thread stacks containing source filenames, function names, and line numbers. Malformed, non-finite, missing, future, or previous-process timestamps are ignored rather than reflected into diagnostics.

The watchdog is diagnostic-only. Failure to start or run it must not stop the media path, and it does not send signals, kill the pipeline, alter retry policy, extend the 45-second smoke timeout, or create a new public egress status.

## Reading a failure

Combine this stack marker with `IRLIGHT_EGRESS_RECONNECT_EVIDENCE` from the reconnect smoke.

- A stack stopped in `_poll_sink()` near the sink stats read makes the GStreamer stats/poll path the leading blocking candidate.
- A stack stopped in `EgressAttempt.run()` at `pipeline.set_state(Gst.State.NULL)` makes synchronous teardown the leading blocking candidate. In that case the outer gateway cannot write `RECONNECTING` until the attempt returns.
- A stack still in the GLib main loop indicates that loop dispatch or the condition needed to quit the attempt has not completed; compare the bounded `rendered` value and status age with the stack location.
- If status remains fresh and rendered progress continues, this watchdog should not fire; the legacy sink counter may still be reporting progress while transport delivery is no longer useful, which is a different failure class.

A stack snapshot is evidence for classification, not by itself a production recovery mechanism. Any runtime fix for #545 should be made only after the blocked boundary is identified and should preserve the existing CONNECTED → outage → retry → recovery contract.
