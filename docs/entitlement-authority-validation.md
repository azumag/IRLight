# Entitlement authority validation

IRLight treats `entitlements.json` as authority for per-user concurrent-session limits. A malformed persisted entitlement must make that authority unavailable rather than being coerced into a default, zero, or effectively unlimited limit.

## Record invariants

The store accepts strict JSON only. `NaN`, `Infinity`, and `-Infinity` are rejected while reading, and serialization uses `allow_nan=False`.

Each persisted entitlement is keyed by a non-empty user ID. The record must contain:

- `id` equal to `user:<user_id>`
- `user_id` equal to the dictionary key
- a non-empty, non-whitespace `plan`
- `max_concurrent_sessions` as a non-negative integer (booleans and numeric strings are not accepted)
- a finite numeric `updated_at` value

The default entitlement returned for a user with no persisted record remains a runtime fallback derived from `IRLIGHT_DEFAULT_MAX_CONCURRENT_SESSIONS`; it is not a persisted record and therefore intentionally has `updated_at: null`.

## Failure and recovery behavior

Invalid persisted authority raises `EntitlementStateError`. Read failures do not rewrite the file, drop the initialization marker, or replace the authority with an empty/default payload. Writers validate the complete in-memory authority before replacement, so a bad timestamp or record cannot overwrite the previous valid file.

Preserve `entitlements.json` and its `.initialized` marker when recovering from corruption. Do not delete the state volume or marker to force a fresh empty authority; the explicit recovery procedure is tracked in #90.

## Scope

This is the entitlement record-validation slice of #87. It deliberately does not invent a maximum supported `max_concurrent_sessions` value or change plan semantics; choosing an explicit upper bound is a separate policy decision. Mapping `EntitlementStateError` to a stable public API failure and the remaining Session/node-authority validation are also tracked separately under #87.
