# Ingest recovery smoke failure diagnostics

The authenticated SRT and RTMPS recovery smokes generate short-lived ingest credentials and embed them in publisher connection strings:

- SRT uses an authenticated `streamid`.
- RTMPS uses `user` / `pass` query parameters.

Publisher, MediaMTX, Control Plane, and Continuity failures can echo connection material. For that reason, the public smoke entrypoints are intentionally split from the implementation:

- `scripts/smoke-srt-ingest-recovery.sh` wraps `scripts/smoke-srt-ingest-recovery-core.sh`.
- `scripts/smoke-rtmps-ingest-recovery.sh` wraps `scripts/smoke-rtmps-ingest-recovery-core.sh`.

The wrapper captures the complete inner stdout/stderr into a run-local private directory created under `umask 077`. A successful run emits only a fixed success message. A failed run emits only a fixed failure annotation with an allowlisted stage token and does not replay, grep, tail, or otherwise copy the captured inner diagnostics into CI output.

The private capture is removed when the wrapper exits. `INT` and `TERM` are forwarded to the running core smoke before cleanup.

The `*-core.sh` files are implementation details. CI and documentation should invoke the public wrapper paths, not the core scripts directly. Dedicated workflows include both wrapper and core paths in their pull-request filters and syntax-check both files so implementation-only changes still receive the recovery E2E gate.

This quarantine is deliberately stricter than best-effort redaction: the outer wrapper never receives the generated credential value, so it cannot accidentally miss an encoded or reformatted copy of that value in an inner diagnostic. If useful failure detail is later reintroduced, it must be reconstructed from an explicit allowlist of non-secret fields rather than replaying the captured log.
