# Node heartbeat inspector clock boundary

`node_heartbeat_inspect_cli.py` is a read-only operational diagnostic. Its output is consumed as machine-readable evidence, so an invalid host wall clock must not turn a state check into an unhandled traceback.

Before opening the Node authority, the CLI validates the effective `time.time()` value. The value must be a numeric, finite, non-negative Unix timestamp. Negative values, `NaN`, positive or negative infinity, invalid types, and numeric conversion failures make the inspector fail closed with the existing redacted `UNAVAILABLE` response and exit code `3`. Unix epoch `0.0` remains valid.

Clock validation does not create locks, initialization markers, authority files, or repaired state. The inspector remains read-only on both success and failure. The heartbeat grace threshold and future-timestamp handling are unchanged: future-skew policy and clock synchronization are operational policy decisions outside this boundary.

This contract is intentionally narrower than Node lifecycle mutation. It protects the diagnostics path used by heartbeat alerting and recovery runbooks without changing Node status, desired state, reaper behavior, or provider resources.
