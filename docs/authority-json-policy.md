# Persisted authority JSON policy

IRLight treats Control Plane state files as authority, not as permissive interchange JSON. Persisted authority readers therefore use `state_safety.load_json_authority()` and fail closed on ambiguous or non-standard JSON instead of guessing a value.

## Reader contract

The shared loader rejects duplicate object keys recursively and rejects the Python JSON decoder's non-standard numeric constants `NaN`, `Infinity`, and `-Infinity` by default. Stores that need a store-specific error message may pass their own `parse_constant` hook, but omitting a hook never opts the authority reader back into accepting non-finite constants.

This matters for stores that previously relied only on the duplicate-key protection of the shared loader. A damaged or restored file containing a non-finite constant now fails through that store's existing `ValueError`/state-error path rather than producing a Python `float` whose comparisons can behave unexpectedly.

## Recovery behavior

Reader rejection does not repair, normalize, truncate, or overwrite the rejected file. Operators should preserve the state file together with its initialization marker and use the explicit recovery workflow tracked in #90. Deleting a marker, replacing the file with an empty object, or recreating the state volume is not a valid recovery shortcut.

Session capacity is part of this authority boundary. `SessionStore` reloads and validates the complete persisted Session set before a prepare transaction can count concurrent sessions. If any capacity-critical Session record is untrustworthy, the user-facing prepare path returns the fixed `SESSION_STATE_UNAVAILABLE` failure before Destination validation, entitlement lookup, or provider selection. The rejected `sessions.json` is not rewritten. This prevents a damaged record from disappearing from the capacity count and indirectly allowing a new billable Media Node to be created. Provider reconciliation for resources that already exist remains a separate recovery concern under #90.

## Writer follow-up

Reader strictness is only one side of the boundary. Individual authority writers should also serialize with `allow_nan=False` and convert serialization failures to their controlled store error before replacing the existing authority file. Stores already audited under #87 keep those guarantees; remaining writers should be migrated independently so a serialization-policy change cannot accidentally alter unrelated record semantics.

The catalog authority writer follows this contract as well. In particular, non-finite probe metadata is rejected as `CatalogStateError` while the temporary file is discarded, so a failed verification result cannot replace the last readable `catalog.json`. This only hardens serialization; it does not add or reinterpret destination or asset record fields.

The destination secret authority follows the same writer rule and also validates the `created_at` / `updated_at` fields that its writer has always persisted. Missing timestamps, booleans, strings, nulls, numeric overflow, and non-finite values fail closed as `DestinationSecretError`; a rejected read does not repair the file, and a rejected write cannot replace the last readable `destination_secrets.json`. No ordering rule is imposed between the two timestamps here because that would change the semantics of the existing caller-supplied `now` hook rather than merely enforce the current record shape.

The legacy audio control authority also follows the writer rule. `control.json` is serialized with `allow_nan=False`, and encoder `TypeError` / `ValueError` failures are converted to `ControlStateError` before `os.replace()`. Its existing record validator remains authoritative for the fixed control schema, including finite `updated_at`; this change does not alter audio-mode, version, idempotency, or command semantics.

The media-node Node Agent uses the same schema when it seeds a fresh local `control.json` from the authenticated bootstrap response. Before the first write it validates the audio mode, non-negative integer version, optional UUID command ID, bounded optional idempotency key, and finite `updated_at` (including numeric-overflow rejection). The seed writer also uses `allow_nan=False`; an invalid bootstrap value or encoder failure cannot publish a partially trusted authority file. Existing local `control.json` remains create-only and is never replaced by bootstrap seeding.

The ingest authentication guard authority follows the same fail-closed boundary. Bucket and event timestamps are non-negative finite numbers; integers too large to convert to a finite runtime timestamp are rejected as `IngestAuthGuardStateError` rather than escaping as an uncontrolled `OverflowError`. Its writer uses `allow_nan=False` and converts encoder `TypeError` / `ValueError` failures before publishing the temporary file, preserving the last readable `ingest_auth_guard.json`. Lockout thresholds, failure-window semantics, and attacker-facing reason behavior are unchanged.

Refs #87 #90
