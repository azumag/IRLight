# Catalog authority API failure contract

The Destination / Asset catalog is persisted authority. A damaged, missing-after-initialization, unreadable, or unwritable `catalog.json` must not be reported to API clients as an ordinary validation or not-found result, and internal authority paths or parser details must not be exposed.

All `/v1/destinations*` and `/v1/assets*` routes therefore translate `CatalogStateError` raised by the catalog store into HTTP `503` with the stable public detail:

```json
{"code":"CATALOG_STATE_UNAVAILABLE"}
```

This mapping is deliberately narrower than the existing route semantics. `CatalogNotFound` remains `404`, caller validation remains `422`, Destination probe admission retains its existing retryable `503` responses, and Destination secret-store configuration has its existing separate unavailable result.

The API layer does not repair, truncate, recreate, or normalize catalog authority when this error is returned. Recovery remains an operator action under the persisted-authority recovery policy. The response contains no state path, persisted record content, destination credential material, or underlying exception text.

Refs #87 #90
