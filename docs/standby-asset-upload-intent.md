# Standby asset upload intent (owner-bound object keys)

Issue #7 requires that a standby asset object key is issued by the server and
bound to its owner, so a caller can never address another owner's namespace, and
that an upload completion is validated before the asset advances. This slice
implements the object-key authority and the upload state transition; it does not
yet implement a storage-backed presigned URL, the processing worker, or the
Media Node prefetch/cache path.

## Object layout

`apps/control-api/asset_upload_intent.py` is the single authority for derived
keys (matching the storage layout proposed in Issue #7):

```text
users/{userId}/assets/{assetId}/source
users/{userId}/assets/{assetId}/variants/720p30.mp4
users/{userId}/assets/{assetId}/variants/1080p30.mp4
```

`source_object_key` / `variant_object_key` accept only printable-ASCII owner and
asset ids with no path separators, no dot-only values, and no leading/trailing
dots, so a segment can never widen, escape, or alias the namespace.

## Upload flow (implemented boundary)

1. `POST /v1/assets/upload-intents` with a declared `content_type`.
2. The server derives the asset id and the object key, and stores the asset in
   `UPLOADING` with `content_type` and `max_bytes`. The caller never supplies the
   key, and the response carries the derived key to place the object under.
3. After uploading, the client calls `POST /v1/assets/{asset_id}/upload-complete`
   with `content_type`, `object_key`, `size_bytes`, and `sha256`.
4. The completion is accepted only if the object key is exactly the owner-bound
   source key, the content type matches the issued intent, the size is a positive
   integer within `MAX_UPLOAD_BYTES` (32 MiB, the same ceiling as the Node-local
   standby bound), and the digest is 64 lowercase hex characters. The asset then
   moves to `PROCESSING`.

Fail-closed outcomes:

- Unsupported media type (including `image/svg+xml`) → `422`.
- Foreign or aliased `object_key` → `422`.
- `size_bytes` of zero, negative, non-integer, or above the cap → `422`.
- Malformed `sha256`, wrong owner, unknown asset, replay, or content-type drift
  → `422` / `404`; the asset never advances.

## Scope boundaries

- **Not implemented here:** presigned URL signing, the processing worker,
  `720p30`/`1080p30` transcoding, immutable variant/version records, Node
  prefetch/cache/LRU, object-storage authentication, and physical deletion. The
  variant key helper is provided so the worker can reuse one authority, but no
  variant is generated yet.
- **Legacy path:** `POST /v1/assets` (`create_asset`) keeps its historical
  metadata-only shape (`processing_status=PENDING`, caller-supplied
  `source_object_key`). It grants no write authority over any key and is retained
  only for compatibility; new uploads must use the upload-intent flow.
- **Validation vocabulary unchanged:** the catalog record validator still treats
  `processing_status` as an arbitrary non-empty string. The writer now also emits
  `UPLOADING` / `PROCESSING` for the upload flow; see
  `docs/catalog-authority-validation.md`.

## Enforcement

The completion check re-derives the expected key from the owner and asset id and
compares it to the stored `source_object_key`, so a persisted record whose key
was tampered with cannot be completed. The asset must currently be `UPLOADING`,
which makes completions single-use and rejects out-of-order or replayed
completions instead of silently re-arming a finished asset.
