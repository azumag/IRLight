# Egress reconnect timeout stack diagnostics

Issue #545 tracks an intermittent legacy `rtmpsink` case where a stopped RTMP target is not reflected as `RECONNECTING` within the existing 45-second smoke contract. The reconnect smoke already records secret-safe status age and rendered-buffer evidence. This diagnostic adds one more boundary without changing egress behavior: when that 45-second contract fails, the smoke asks the still-running gateway to dump Python thread stacks before cleanup captures its logs.

## Trigger and signal boundary

The stack signal hook is opt-in. Production keeps its previous signal behavior unless `EGRESS_STACK_SIGNAL_DIAGNOSTICS=1` is explicitly supplied by a diagnostic harness. The reconnect smoke supplies that value to its isolated egress gateway.

When enabled, the egress entrypoint registers `SIGUSR2` with Python `faulthandler` and emits the fixed readiness marker:

```text
IRLIGHT_EGRESS_STACK_SIGNAL_READY signal=SIGUSR2
```

The reconnect smoke sends `SIGUSR2` only after all of the following are true:

- the existing `wait_egress_status RECONNECTING 45` check has failed;
- the egress gateway container is still running;
- recent redacted gateway logs contain the readiness marker confirming that the handler is armed.

If any guard fails, the smoke skips the signal and continues normal failure evidence collection. The signal is therefore never sent on a passing reconnect path, and it is never sent to a process that has not positively confirmed the handler. The existing 45-second timeout is not extended.

`faulthandler` is implemented in C and its traceback output is designed for failures and deadlocks. This is preferable to a Python polling thread for #545 because the suspected blocked boundary may itself prevent another Python thread from running promptly.

## Secret boundary

The signal handler does not print the status JSON, destination URL, destination host, stream key, plugin error text, or Python local variables. `faulthandler` emits traceback metadata containing source filenames, function names, and line numbers. The smoke's readiness and request/skip markers are fixed strings and do not include generated credentials. Failure cleanup still passes the larger gateway log window through the existing stream-key redactor before writing it to CI output.

The handler is diagnostic-only. Registration failure must not stop the media path. Receiving the registered signal does not terminate the gateway, kill the pipeline, alter retry policy, extend the reconnect timeout, or create a new public egress status.

## Reading a failure

Combine the resulting traceback with `IRLIGHT_EGRESS_RECONNECT_EVIDENCE` from the reconnect smoke.

- A stack stopped in `_poll_sink()` near the sink stats read makes the GStreamer stats/poll path the leading blocking candidate.
- A stack stopped in `EgressAttempt.run()` at `pipeline.set_state(Gst.State.NULL)` makes synchronous teardown the leading blocking candidate. In that case the outer gateway cannot write `RECONNECTING` until the attempt returns.
- A stack still in the GLib main loop indicates that loop dispatch or the condition needed to quit the attempt has not completed; compare the bounded `rendered` value and status age with the stack location.
- If `IRLIGHT_EGRESS_RECONNECT_EVIDENCE` shows a fresh status and rendered progress continues through the outage, the legacy sink counter may still be reporting progress while transport delivery is no longer useful, which is a different failure class.

A stack snapshot is evidence for classification, not by itself a production recovery mechanism. Any runtime fix for #545 should be made only after the blocked boundary is identified and should preserve the existing CONNECTED → outage → retry → recovery contract.
