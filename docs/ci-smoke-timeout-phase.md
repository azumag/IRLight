# Docker smoke outer-timeout phase evidence

The shared Docker smoke suite bounds every scenario with an outer 180-second timeout. A scenario can therefore be terminated before its normal `emit_failure_stage()` path runs. In that case an ordinary failure-stage annotation is unavailable even though the scenario was still in a known, safe phase.

`scripts/smoke-egress-reconnect.sh` and `scripts/smoke-egress-stop-terminal.sh` emit a fixed-vocabulary marker before each major blocking phase:

```text
IRLIGHT_DOCKER_SMOKE_PHASE phase=<token>
```

These markers contain only hard-coded ASCII tokens. They never contain a destination URL, stream key, raw status, container log, command line, project/container identifier, or arbitrary external input.

`scripts/ci-docker-smoke-suite.sh` continues to prefer the normal explicit `::error ... stage=<token>` annotation. Only when the scenario exits with an outer-timeout status (`124` or `137`) and no explicit stage was emitted does the harness read the last complete phase-marker line and report it as `timeout-<phase>` in the compact result and GitHub Step Summary.

The parser accepts only a complete line matching `[A-Za-z0-9._-]+`. A marker with extra characters or a secret-like suffix is ignored rather than truncated into a valid-looking token. If no valid marker exists, the existing `stage=-` behavior remains.

For the stop-terminal smoke, markers cover the initial Compose bring-up, initial connected wait, remote-target stop and reconnect wait, user-stop path, target recovery observation, terminal unsafe-destination probe, and final generated-secret checks. The marker is diagnostic only: the existing explicit failure stage still wins whenever the smoke reaches its normal error path.

This diagnostic does not extend the 180-second scenario bound, the legacy 45-second `RECONNECTING` contract, the 5-second gateway stop timeout, or any runtime retry/stall policy. It only makes outer-timeout evidence durable enough to distinguish which major egress-smoke phase was active when the process was terminated.
