# Ingest credential authority validation

`apps/control-api/ingest_store.py` treats `ingest_credentials.json` as authentication authority. A malformed record is therefore not repaired, skipped, or interpreted as an empty store: loading fails closed with `IngestCredentialError` and the existing file is left untouched.

## Record contract

Each credential entry must have a non-empty string key and a dictionary record whose `id` matches that key. `session_id`, `user_id`, `username`, and `secret_sha256` are required non-empty strings; `username` must match `session_id`, and the stored SHA-256 digest must be a 64-character hexadecimal value.

`protocols` must be a non-empty list containing only supported protocol names (`rtmp` and/or `srt`). Persisted `scope` values must be `INGEST` or `RELAY_CLIENT`. Credentials created before relay support did not persist `scope`, so an absent scope is intentionally interpreted as legacy `INGEST`; unknown or malformed scopes are rejected rather than guessed.

`created_at` and `expires_at` must be finite JSON numbers and `expires_at` must be later than `created_at`. Optional `revoked_at` and `last_authenticated_at` values, when present, must also be finite numbers. Boolean, string, null (for required timestamps), `NaN`, and positive/negative infinity are rejected. JSON reads reject non-standard non-finite constants, and writes use `allow_nan=False`.

## Failure and recovery behavior

Validation happens before an authority file is accepted and before an updated in-memory authority is persisted. A validation failure does not rewrite the authority, remove the initialization fuse, or create a replacement empty file. Operators should preserve the invalid file for diagnosis and restore a known-good authority backup or perform an explicit reviewed migration.

The store intentionally does not silently discard only the malformed credential. Doing so could change which publisher credential is authoritative or accidentally resurrect a credential that should have been revoked.

## Tests

`tests/test_ingest_authority_validation.py` covers non-finite timestamps, type confusion, missing required fields, identity/digest/protocol/scope invariants, preservation of legacy pre-relay records without `scope`, non-destructive failure on invalid authority, and rejection of non-finite issuance time inputs before persistence.

This is one record-level slice of the broader persistent-state hardening tracked in #87. Session, entitlement, and node authority validation remain separate concerns.
