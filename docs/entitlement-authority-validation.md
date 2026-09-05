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

## API failure contract

Session prepare treats entitlement-authority failures as service-availability failures. If `default_entitlement_store()` cannot initialize or `EntitlementStore.get()` raises `EntitlementStateError`, `POST /v1/sessions/{session_id}/prepare` returns HTTP 503 with the stable public detail `{"code":"ENTITLEMENT_STATE_UNAVAILABLE"}`.

The public response deliberately omits the authority path and underlying exception text. The failure is raised before `begin_prepare` reserves capacity and before a provider is selected, so malformed or unreadable entitlement state cannot trigger provisioning or a billable-node allocation attempt.

Committed prepare replays remain replayable without rereading entitlement authority, preserving the existing idempotency boundary for already-committed responses.

## Scope

This is the entitlement record-validation and API fail-closed slice of #87. It deliberately does not invent a maximum supported `max_concurrent_sessions` value or change plan semantics; choosing an explicit upper bound is a separate policy decision. Remaining Session/node-authority validation and regression coverage preventing corrupt Session state from affecting capacity or paid-node provisioning are tracked separately under #87.
