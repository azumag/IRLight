# Catalog authority record validation

`catalog.json` is authoritative state for Destination and standby Asset records. Reads fail closed with `CatalogStateError` when the persisted record shape cannot have been emitted safely by the current writer.

## Enforced record invariants

For every Destination and Asset, the dictionary key and persisted `id` must be the same non-empty string and `user_id` must be a non-empty string. This prevents damaged ownership or identity metadata from being treated as an ordinary not-found record or being returned to API callers.

Destination records additionally require non-empty string values for `type`, `display_name`, `server_url`, and `secret_ref`. `server_url` continues to pass through the existing credential-material safety validator. A malformed or non-string URL therefore cannot bypass the URL safety check, and a malformed `secret_ref` cannot reach the destination secret-store boundary through string coercion.

Asset records require a non-empty string `source_object_key` in addition to the shared identity fields.

Validation is read-only. A rejected `catalog.json` is not repaired, rewritten, normalized, or replaced with an empty catalog. The public `/v1/destinations*` and `/v1/assets*` API boundary maps the resulting `CatalogStateError` to the stable `503` reason `CATALOG_STATE_UNAVAILABLE`.

## Deliberately unchanged

This validation does not define new product policy. Destination protocol vocabulary, verification-state vocabulary, Asset processing-state vocabulary, timestamp bounds, retention, provider behavior, billing, secret values, and migration/repair policy are unchanged. Further tightening of those fields requires compatibility review against persisted production state before it is made authoritative.
