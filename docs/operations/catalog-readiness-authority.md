# Catalog readiness authority validation

`/readyz` treats `catalog.json` as startup authority and now reuses the same pure record-level validator as `CatalogStore` request paths. This keeps readiness aligned with the persisted Destination and Asset authority contract instead of maintaining a weaker duplicate validator.

Readiness therefore fails closed when catalog identity or ownership metadata is malformed, when Destination secret-boundary fields are not valid non-empty strings, when an Asset `source_object_key` is malformed, or when a Destination URL contains embedded credential material. The public HTTP result remains the stable `503` reason code `STATE_AUTHORITY_UNAVAILABLE`; record contents, filesystem paths, parser details, and secret references are not exposed.

The shared validation call is read-only. Readiness does not acquire the catalog store lock, create or rewrite authority, repair records, invoke the destination secret store, probe network destinations, or call provider APIs. Recovery remains an explicit operator action.

This synchronization does not introduce new protocol, verification-status, processing-status, timestamp, retention, migration, provider, billing, or secret-value policy. Those remain separate compatibility decisions.
