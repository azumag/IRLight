# Session capacity authority time boundary

`session_capacity_inspect_cli.py` is a read-only operator tool used to decide whether concurrent Session capacity can be trusted. It must not turn malformed lifecycle evidence into an apparently free slot.

## Pre-epoch timestamps

Session lifecycle timestamps are Unix-time authority. Finite values below `0` are invalid evidence and fail closed. The boundary covers:

- Session `created_at` / `updated_at` and the optional lifecycle timestamps validated by `SessionStore`;
- retained Session event `occurred_at` values;
- cleanup lease `created_at` / `expires_at` values.

Unix epoch `0.0` remains valid. A rejected persisted timestamp is not repaired, normalized, deleted, or rewritten; operators must preserve the original authority and follow the state-recovery procedure.

The read-only capacity inspector reports invalid lifecycle evidence as `CAPACITY_UNAVAILABLE` with `authority=sessions`. Its public output remains aggregate-only and does not expose the user ID, Session ID, state path, raw timestamp, or parser exception. `CAPACITY_UNAVAILABLE` must never be interpreted as `occupied=0` or as permission to provision a new paid resource.

## Writer clock boundary

`SessionStore` applies the same finite, non-negative boundary to timestamps it creates or accepts for mutation. Default `time.time()` clocks, explicit caller timestamps, retained event times, node/ingest observations, provisioning timestamps, and timestamp-bearing lifecycle changes are validated before the corresponding record is mutated.

Derived authority is validated after arithmetic as well. In particular, absolute Session deadlines and cleanup-lease expiries must still be finite and non-negative after adding their duration. An overflow or invalid clock raises `SessionStateError` before `sessions.json` is created or replaced.

This writer guard does not introduce a future-clock skew limit or new timestamp-ordering rules such as `updated_at >= created_at`. Those remain separate specification decisions. Existing status vocabulary, lifecycle transitions, entitlement behavior, cleanup ownership semantics, and provider behavior are unchanged.

## Recovery boundary

A malformed persisted timestamp remains an authority error rather than an auto-repair trigger. The existing capacity-exhaustion and state-recovery runbooks remain authoritative for operator response. Writer-side validation prevents new pre-epoch/non-finite timestamp authority from being published, while the read-only inspector independently refuses to treat already-damaged authority as free capacity.
