# Catalog authority record validation

`catalog.json` is authoritative state for Destination and standby Asset records. Reads fail closed with `CatalogStateError` when the persisted record shape cannot have been emitted safely by the current writer.

## Enforced record invariants

For every Destination and Asset, the dictionary key and persisted `id` must be the same non-empty string and `user_id` must be a non-empty string. This prevents damaged ownership or identity metadata from being treated as an ordinary not-found record or being returned to API callers.

Destination records additionally require non-empty string values for `type`, `display_name`, `server_url`, and `secret_ref`. `server_url` continues to pass through the existing credential-material safety validator. A malformed or non-string URL therefore cannot bypass the URL safety check, and a malformed `secret_ref` cannot reach the destination secret-store boundary through string coercion.

Destination operational metadata must also match the current writer's structural shape. `enabled` is a boolean and `verification_status` is a non-empty string. `last_verified_at` must be present and is either null or a finite numeric timestamp; `last_verification_error` must be present and is either null or a string; `verification_transport` must be present and is either null or an object. These checks deliberately validate representation only: they do not introduce a new verification-state vocabulary or a closed transport schema.

Asset records require non-empty string values for both `source_object_key` and `processing_status` in addition to the shared identity fields. The processing-state vocabulary remains unchanged; this only rejects missing, empty, or non-string persisted values that the current writer does not emit.

Both Destination and Asset records require finite numeric `created_at` and `updated_at` values matching the shape emitted by the existing writers. Missing timestamps, booleans, strings, nulls, non-finite values, and integers that overflow finite float normalization are rejected as `CatalogStateError`. The same validator runs before persistence, so invalid in-memory timestamps or operational metadata cannot replace the last readable catalog.

Validation is read-only. A rejected `catalog.json` is not repaired, rewritten, normalized, or replaced with an empty catalog. The public `/v1/destinations*` and `/v1/assets*` API boundary maps the resulting `CatalogStateError` to the stable `503` reason `CATALOG_STATE_UNAVAILABLE`.

## Deliberately unchanged

This validation does not define new product policy. Destination protocol vocabulary, the allowed values of verification and Asset processing states, detailed verification transport semantics, timestamp ordering or semantic range, retention, provider behavior, billing, secret values, and migration/repair policy are unchanged. Further tightening of those semantics requires compatibility review against persisted production state before it is made authoritative.
