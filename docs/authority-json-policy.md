# Persisted authority JSON policy

IRLight treats Control Plane state files as authority, not as permissive interchange JSON. Persisted authority readers therefore use `state_safety.load_json_authority()` and fail closed on ambiguous or non-standard JSON instead of guessing a value.

## Reader contract

The shared loader rejects duplicate object keys recursively and rejects the Python JSON decoder's non-standard numeric constants `NaN`, `Infinity`, and `-Infinity` by default. Stores that need a store-specific error message may pass their own `parse_constant` hook, but omitting a hook never opts the authority reader back into accepting non-finite constants.

This matters for stores that previously relied only on the duplicate-key protection of the shared loader. A damaged or restored file containing a non-finite constant now fails through that store's existing `ValueError`/state-error path rather than producing a Python `float` whose comparisons can behave unexpectedly.

## Recovery behavior

Reader rejection does not repair, normalize, truncate, or overwrite the rejected file. Operators should preserve the state file together with its initialization marker and use the explicit recovery workflow tracked in #90. Deleting a marker, replacing the file with an empty object, or recreating the state volume is not a valid recovery shortcut.

Session capacity is part of this authority boundary. `SessionStore` reloads and validates the complete persisted Session set under its process-safe lock before a prepare transaction reserves concurrent-session capacity. Corruption already present on the initial prepare read returns the fixed `SESSION_STATE_UNAVAILABLE` response before Destination validation, entitlement lookup, or provider selection. If the authority becomes corrupt after that replay read, `begin_prepare()` reloads it again before the capacity decision and still fails before provider selection. Rejection does not rewrite `sessions.json`. These checks prevent a damaged record from disappearing from the capacity count and indirectly allowing a new billable Media Node to be created. Provider reconciliation for resources that already exist remains a separate recovery concern under #90.

## Writer follow-up

Reader strictness is only one side of the boundary. Individual authority writers should also serialize with `allow_nan=False` and convert serialization failures to their controlled store error before replacing the existing authority file. Stores already audited under #87 keep those guarantees; remaining writers should be migrated independently so a serialization-policy change cannot accidentally alter unrelated record semantics.

The catalog authority writer follows this contract as well. In particular, non-finite probe metadata is rejected as `CatalogStateError` while the temporary file is discarded, so a failed verification result cannot replace the last readable `catalog.json`. This only hardens serialization; it does not add or reinterpret destination or asset record fields.

The destination secret authority follows the same writer rule and also validates the `created_at` / `updated_at` fields that its writer has always persisted. Missing timestamps, booleans, strings, nulls, numeric overflow, and non-finite values fail closed as `DestinationSecretError`; a rejected read does not repair the file, and a rejected write cannot replace the last readable `destination_secrets.json`. No ordering rule is imposed between the two timestamps here because that would change the semantics of the existing caller-supplied `now` hook rather than merely enforce the current record shape.

The legacy audio control authority also follows the writer rule. `control.json` is serialized with `allow_nan=False`, and encoder `TypeError` / `ValueError` failures are converted to `ControlStateError` before `os.replace()`. Its fixed control schema also rejects boolean, non-finite, and float-overflowing `updated_at` values through `ControlStateError`, including caller-supplied write timestamps, before the existing authority can be replaced. This change does not alter audio-mode, version, idempotency, or command semantics.

The Continuity service treats that same `control.json` as an authority input rather than permissive application JSON. Its isolated runtime reader rejects duplicate object keys and the non-standard `NaN` / `Infinity` constants before schema validation, rejects integer timestamps that overflow float normalization instead of allowing an uncaught `OverflowError` to escape the reconcile path, and applies the Control Store's existing UUID command-id and bounded idempotency-key shape checks even though Continuity does not consume the idempotency key. An invalid read preserves the last valid command; before any valid command has been observed the existing fail-safe remains `MUTED`. The Continuity image keeps this small decoder/schema guard local rather than importing Control Plane runtime modules, so its current packaging and `PYTHONPATH=/app` boundary do not change.

The media-node Node Agent uses the same schema when it seeds a fresh local `control.json` from the authenticated bootstrap response. Before the first write it validates the audio mode, non-negative integer version, optional UUID command ID, bounded optional idempotency key, and finite `updated_at` (including numeric-overflow rejection). The seed writer also uses `allow_nan=False`; an invalid bootstrap value or encoder failure cannot publish a partially trusted authority file. Existing local `control.json` remains create-only and is never replaced by bootstrap seeding.

The ingest authentication guard authority follows the same fail-closed boundary. Bucket and event timestamps are non-negative finite numbers; integers too large to convert to a finite runtime timestamp are rejected as `IngestAuthGuardStateError` rather than escaping as an uncontrolled `OverflowError`. Its writer uses `allow_nan=False` and converts encoder `TypeError` / `ValueError` failures before publishing the temporary file, preserving the last readable `ingest_auth_guard.json`. Lockout thresholds, failure-window semantics, and attacker-facing reason behavior are unchanged.

The file-backed fake provider inventory used by multi-process dry-run and lifecycle tests follows the same fail-closed JSON discipline even though it is not the production cloud provider source of truth. `FileFakeProvider` rejects malformed JSON, duplicate object keys, non-standard non-finite constants, and records that do not match the shape produced by its own writer instead of silently treating a damaged file as an empty provider inventory. Its writer validates that shape, uses `allow_nan=False`, and discards the temporary file on validation or serialization failure so the last readable fake inventory remains available. This prevents local reaper/lifecycle tests from masking state corruption as “no provider resources”.

Refs #87 #90
