# SRT destination URL secret safety

IRLight treats a Destination `server_url` as non-secret configuration. The value is stored in the catalog and can be returned by the owner-facing Destination API, so credentials must never be embedded in it.

## Supported SRT URL shape

Routing-only stream IDs remain supported. For example:

```text
srt://stream.example.test:8890?streamid=publish:probe&latency=120
```

The Destination URL query is deliberately narrow: `streamid`, `latency`, `mode`, and `conntimeo` are the only accepted query names. `mode` must still be `caller` when explicitly supplied, and the verifier replaces `mode` and `conntimeo` with its own controlled values before spawning the child process. Unknown or duplicate query names are rejected rather than forwarded to the SRT implementation.

The verifier may pass accepted non-secret routing information to `srt-live-transmit` while pinning the resolved target IP and forcing caller mode.

## Credential-bearing forms

The following forms are rejected before catalog persistence and before a verifier process can be spawned:

- URL userinfo (`user:password@host`)
- SRT `passphrase` query parameters
- IRLight/MediaMTX authenticated stream IDs such as `publish:<path>:<user>:<credential>`
- structured SRT stream IDs containing credential fields such as `u`, `username`, `password`, `passphrase`, `secret`, or `token`
- encoded equivalents of the above, including repeated percent encoding
- duplicate query parameters whose interpretation could differ between validation and the SRT implementation
- query parameters outside the explicit public allowlist

Validation errors never include the credential value.

## Authenticated SRT destination probing

Authenticated SRT Destination verification is intentionally unsupported until IRLight has a delivery path that can provide the credential to the SRT implementation without placing it in process arguments, environment variables, ordinary logs, or owner-visible catalog fields.

Destination secrets continue to belong in the Destination Secret store. Do not work around the restriction by copying a secret back into `server_url`.

This restriction applies to **Destination verification**. It does not change the existing ingest connection-info format used by publishers to authenticate when sending media into IRLight.

## Existing unsafe catalog records

If an older `catalog.json` already contains a credential-bearing SRT URL, catalog reads fail closed rather than returning the record or silently rewriting it. IRLight does not delete the record, erase initialization markers, or replace the catalog with an empty file.

Recovery procedure:

1. Quiesce every Control Plane writer that shares the same `STATE_DIR`.
2. Back up `catalog.json` together with its initialization marker and record file ownership/permissions.
3. Identify the affected Destination without copying the credential into tickets, logs, or shell history.
4. Remove credential material from `server_url`; keep only the non-secret routing URL.
5. Configure a replacement credential through the Destination Secret store when the selected protocol supports a safe delivery path.
6. Rotate the credential that was previously embedded in the catalog URL.
7. Restart/read the catalog and confirm the Destination can be listed without exposing credential material.

Do not recover by deleting the initialization marker, creating an empty catalog, or running destructive volume cleanup.

## Follow-up boundary

This guard prevents persistence and process-argument exposure. It does not implement an authenticated SRT probe helper, global probe execution budgets, or SRT provider-specific authentication semantics. Those remain separate implementation work.