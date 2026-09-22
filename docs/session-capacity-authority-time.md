# Session capacity authority time boundary

`session_capacity_inspect_cli.py` is a read-only operator tool used to decide whether concurrent Session capacity can be trusted. It must not turn malformed lifecycle evidence into an apparently free slot.

## Pre-epoch timestamps

The capacity inspector treats persisted Session lifecycle timestamps as Unix-time authority. Finite values below `0` are invalid evidence and cause the inspector to fail closed with `CAPACITY_UNAVAILABLE` and `authority=sessions`.

The guard covers:

- Session `created_at` / `updated_at` and the optional lifecycle timestamps validated by `SessionStore`;
- retained Session event `occurred_at` values;
- cleanup lease `created_at` / `expires_at` values.

Unix epoch `0.0` remains valid. The inspector does not repair, normalize, delete, or rewrite a rejected `sessions.json`; operators must preserve the original authority and follow the state-recovery procedure.

The public CLI output remains aggregate-only. A rejected timestamp does not expose the user ID, Session ID, state path, raw timestamp, or parser exception.

## Scope

This guard hardens the read-only capacity decision boundary tracked by Issue #87. It deliberately does **not** claim that all `SessionStore` writer clocks are hardened yet. The lifecycle store still needs its own shared non-negative writer-clock validation so a pre-epoch system clock cannot publish invalid Session authority in the first place.

Until that writer-side work lands, `CAPACITY_UNAVAILABLE` must never be interpreted as `occupied=0` or as permission to provision a new paid resource. The existing capacity-exhaustion runbook remains authoritative for operator response.
