# Persisted authority JSON policy

IRLight treats Control Plane state files as authority, not as permissive interchange JSON. Persisted authority readers therefore use `state_safety.load_json_authority()` and fail closed on ambiguous or non-standard JSON instead of guessing a value.

## Reader contract

The shared loader rejects duplicate object keys recursively and rejects the Python JSON decoder's non-standard numeric constants `NaN`, `Infinity`, and `-Infinity` by default. Stores that need a store-specific error message may pass their own `parse_constant` hook, but omitting a hook never opts the authority reader back into accepting non-finite constants.

This matters for stores that previously relied only on the duplicate-key protection of the shared loader. A damaged or restored file containing a non-finite constant now fails through that store's existing `ValueError`/state-error path rather than producing a Python `float` whose comparisons can behave unexpectedly.

Numeric validation must also treat conversion overflow as invalid authority. JSON integers are arbitrary precision in Python, so values such as `10**400` can parse successfully but overflow when converted to `float`. Store validators must turn that case into their controlled state error rather than allowing `OverflowError` to escape. Readers outside the Control Plane process that consume the same authority must preserve the same fail-closed behavior.

## Recovery behavior

Reader rejection does not repair, normalize, truncate, or overwrite the rejected file. Operators should preserve the state file together with its initialization marker and use the explicit recovery workflow tracked in #90. Deleting a marker, replacing the file with an empty object, or recreating the state volume is not a valid recovery shortcut.

## Writer follow-up

Reader strictness is only one side of the boundary. Individual authority writers should also serialize with `allow_nan=False` and convert serialization failures to their controlled store error before replacing the existing authority file. Stores already audited under #87 keep those guarantees; remaining writers should be migrated independently so a serialization-policy change cannot accidentally alter unrelated record semantics.

The catalog authority writer follows this contract as well. In particular, non-finite probe metadata is rejected as `CatalogStateError` while the temporary file is discarded, so a failed verification result cannot replace the last readable `catalog.json`. This only hardens serialization; it does not add or reinterpret destination or asset record fields.

The destination secret authority follows the same writer rule and also validates the `created_at` / `updated_at` fields that its writer has always persisted. Missing timestamps, booleans, strings, nulls, numeric overflow, and non-finite values fail closed as `DestinationSecretError`; a rejected read does not repair the file, and a rejected write cannot replace the last readable `destination_secrets.json`. No ordering rule is imposed between the two timestamps here because that would change the semantics of the existing caller-supplied `now` hook rather than merely enforce the current record shape.

The audio control authority applies the same rule to `updated_at`. Bool/string/null, non-finite values, and arbitrary-precision integers that cannot be represented as finite floats fail closed as `ControlStateError`; rejected updates cannot replace the last readable `control.json`. The Continuity reader applies the same overflow handling and preserves its last valid command, or starts fail-safe in `MUTED`, rather than propagating a numeric conversion exception into the media process.

Refs #87 #90
