# Node Agent deadline clock validation

The Node Agent treats the bootstrap `absolute_deadline` and every wall-clock sample used to enforce it as a fail-closed trust boundary.

## Bootstrap contract

When `absolute_deadline` is present in the bootstrap response it must be a numeric, finite, non-negative Unix timestamp. Booleans, strings, `NaN`, positive or negative infinity, negative values, and values that cannot be converted to a finite float are rejected. A missing deadline keeps the existing no-deadline behavior, and Unix epoch `0.0` remains valid.

## Runtime contract

Before media start, before a heartbeat computes `deadline_remaining_seconds`, and inside the deadline watchdog, the Agent validates the local `time.time()` sample as finite and non-negative. Deadline subtraction is also checked before its result is used.

An invalid pre-start clock prevents media start. An invalid heartbeat deadline clock prevents heartbeat publication. An invalid watchdog clock is treated as a safety failure and stops the media stack instead of silently disabling deadline enforcement.

This validation intentionally does not add a future-skew limit or change the configured deadline duration/policy. It only prevents malformed timestamps and broken wall-clock samples from bypassing the existing deadline safety boundary.
