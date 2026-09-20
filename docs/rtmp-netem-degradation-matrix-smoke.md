# RTMP netem degradation matrix smoke

The public CI entry point is `scripts/smoke-rtmp-netem-degradation-matrix.sh`. The credential-bearing implementation remains in `scripts/smoke-rtmp-netem-degradation-matrix-core.sh`.

The wrapper creates a `umask 077` run-local directory, captures all core stdout/stderr there, forwards `INT`/`TERM`, removes the private directory on exit, and never replays the captured diagnostics. CI sees only fixed failure stages and allowlisted success markers derived from validated profile names after the core exits successfully.

The burst-loss workflow still proves that the requested loss correlation reached the actual `tc netem` state. The raw `tc` output stays quarantined; after a successful core run the wrapper inspects the private log for the expected loss/correlation pair and emits only `loss-correlation=<percent> verified=yes` when the proof matches.

The latency-level harness still exercises 50/100/300/1000 ms RTMP profiles without bypassing the public boundary. It derives a temporary core from the behavior implementation and passes that private core path to the wrapper through `RTMP_NETEM_MATRIX_CORE`; the wrapper continues to quarantine all generated-core output. SRT behavior is unchanged.

The behavioral contract remains in the core: all 1/3/5/10% loss, latency+jitter, and bandwidth profiles must keep the relay online and the publisher supervisor alive, the Session must remain nonterminal, any HOLDING transition must recover, and Continuity must return to LIVE after each profile.
