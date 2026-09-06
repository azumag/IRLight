# Session authority validation

`sessions.json` is authoritative for both lifecycle recovery and concurrent-session capacity. A record that cannot be trusted must therefore stop Session operations instead of being interpreted as inactive, terminal, or free capacity.

## Load and write contract

`SessionStore` rejects non-standard JSON numeric constants (`NaN`, `Infinity`, `-Infinity`) while loading and writes with `allow_nan=False`. The store validates records both after load and before replacement of the authority file, so an invalid in-memory update cannot publish a new authority file.

Each persisted Session must have a non-empty key, matching `session_id`, a non-empty `user_id`, a known `status`, a non-negative integer `version`, boolean `cleanup_pending`, and finite numeric `created_at` / `updated_at`. Present lifecycle timestamps must be finite numbers (or `null` where the field is optional). Present lifecycle booleans and counters are type-checked so Python truthiness or numeric coercion cannot change capacity or recovery behavior. Cleanup leases in the same authority file are also validated for scope, identity, finite increasing expiry, and scope-specific fields; a corrupt lease is never silently treated as expired.

Unknown Session fields remain preserved for forward-compatible metadata; this validation is intentionally focused on fields that affect identity, capacity, state-machine transitions, and timeout/recovery decisions.

## Compatibility

`entitlement_reserved` was added with the concurrent-session entitlement feature after Session persistence already existed. Records written before that release can legitimately omit both `entitlement_id` and `entitlement_reserved`. Missing `entitlement_reserved` is therefore accepted only as a documented legacy shape. Active legacy sessions are still counted through their validated `status in CAPACITY_STATES`, so deployment cannot create a free capacity slot for an already-running Session.

Other validated fields that existed from the first Session schema (`session_id`, `user_id`, `status`, `version`, `cleanup_pending`, `created_at`, `updated_at`) are required. Fields introduced later are validated when present rather than synthesized during load.

## Recovery

Do not delete the initialization marker, replace a corrupt record with `{}`, or change an unknown status to `STOPPED` merely to restore service. Those actions can hide provider resources and undercount capacity. Restore the last known-good `sessions.json` from backup or reconcile the record against provider/node evidence first, then restart the control plane. The store does not rewrite a rejected authority file.

The node authority remains a separate validation surface under issue #87.
