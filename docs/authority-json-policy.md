# Persisted authority JSON policy

IRLight treats Control Plane state files as authority, not as permissive interchange JSON. Persisted authority readers therefore use `state_safety.load_json_authority()` and fail closed on ambiguous or non-standard JSON instead of guessing a value.

## Reader contract

The shared loader rejects duplicate object keys recursively and rejects the Python JSON decoder's non-standard numeric constants `NaN`, `Infinity`, and `-Infinity` by default. Stores that need a store-specific error message may pass their own `parse_constant` hook, but omitting a hook never opts the authority reader back into accepting non-finite constants.

This matters for stores that previously relied only on the duplicate-key protection of the shared loader. A damaged or restored file containing a non-finite constant now fails through that store's existing `ValueError`/state-error path rather than producing a Python `float` whose comparisons can behave unexpectedly.

## Recovery behavior

Reader rejection does not repair, normalize, truncate, or overwrite the rejected file. Operators should preserve the state file together with its initialization marker and use the explicit recovery workflow tracked in #90. Deleting a marker, replacing the file with an empty object, or recreating the state volume is not a valid recovery shortcut.

## Writer follow-up

Reader strictness is only one side of the boundary. Individual authority writers should also serialize with `allow_nan=False` and convert serialization failures to their controlled store error before replacing the existing authority file. Stores already audited under #87 keep those guarantees; remaining writers should be migrated independently so a serialization-policy change cannot accidentally alter unrelated record semantics.

Refs #87 #90
