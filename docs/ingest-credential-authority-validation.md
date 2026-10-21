# Ingest credential authority validation

`apps/control-api/ingest_store.py` treats `ingest_credentials.json` as authentication authority. A malformed record is therefore not repaired, skipped, or interpreted as an empty store: loading fails closed with `IngestCredentialError` and the existing file is left untouched.

## Record contract

Each credential entry must have a non-empty string key and a dictionary record whose `id` matches that key. `session_id`, `user_id`, `username`, and `secret_sha256` are required non-empty strings; `username` must match `session_id`. The stored SHA-256 digest must be the canonical 64-character lowercase hexadecimal form emitted by `hashlib.sha256(...).hexdigest()`. Uppercase or otherwise non-canonical digests are rejected rather than treated as a second representation of the same authority value.

`protocols` must be a non-empty canonical list containing only supported protocol names (`rtmp` and/or `srt`): values are lowercase, duplicate-free, and sorted. `issue()` already normalizes caller input to this representation before persistence, so a duplicate or reordered persisted list cannot be produced by the normal writer and is treated as damaged/unsupported authority. Persisted `scope` values must be `INGEST` or `RELAY_CLIENT`. Credentials created before relay support did not persist `scope`, so an absent scope is intentionally interpreted as legacy `INGEST`; unknown or malformed scopes are rejected rather than guessed.

`created_at` and `expires_at` must be finite, non-negative JSON numbers and `expires_at` must be later than `created_at`. Optional `revoked_at` and `last_authenticated_at` values, when present, must also be finite and non-negative. Boolean, string, null (for required timestamps), negative values, `NaN`, and positive/negative infinity are rejected. JSON reads reject non-standard non-finite constants, and writes use `allow_nan=False`.

Runtime credential clocks use the same non-negative wall-clock boundary. `issue()`, `verify()`, `active_for_session()`, and `revoke_session()` reject a pre-epoch effective `now`; `revoke()` validates its default `time.time()` before mutating an active credential. Unix epoch `0.0` remains valid. Issuance also validates the derived `expires_at` before generating the raw publisher secret, so finite inputs whose addition overflows to a non-finite expiry fail before any credential material or authority mutation is created.

## Issuance endpoint clock boundary

The user-facing issuance endpoint (`issue_ingest_credential` in `apps/control-api/ingest_api.py`) caps the requested credential TTL at the Session's persisted `absolute_deadline_at`. It now pins and validates the effective wall clock once, before that comparison, as a finite non-negative number. An invalid clock (`NaN`, `±Infinity`, negative, boolean, string, or integer that overflows float normalization) fails closed with the fixed HTTP 503 `ingest authority clock unavailable` before any credential is created or rotated, instead of silently skipping the deadline comparison or surfacing an unhandled exception.

The persisted deadline itself is re-validated at the enforcement point: a value that is not a finite non-negative number fails closed with the fixed HTTP 503 `ingest deadline authority unavailable`. A damaged deadline is therefore never interpreted as "no deadline", which would let a credential outlive the Session it belongs to. A valid future deadline still caps the TTL, and the exact deadline instant (`deadline <= now`) is already expired. The validated clock is also passed to `issue()` as `now`, so the credential's `created_at` / `expires_at` share the single checked sample instead of re-reading the wall clock.

## Failure and recovery behavior

Validation happens before an authority file is accepted and before an updated in-memory authority is persisted. A validation failure does not rewrite the authority, remove the initialization fuse, or create a replacement empty file. Operators should preserve the invalid file for diagnosis and restore a known-good authority backup or perform an explicit reviewed migration.

The store intentionally does not silently discard only the malformed credential. Doing so could change which publisher credential is authoritative or accidentally resurrect a credential that should have been revoked. Non-canonical digest/protocol encodings are likewise not normalized while reading: authority migration is an explicit operator action, not an implicit side effect of startup.

## Tests

`tests/test_ingest_authority_validation.py` covers non-finite and pre-epoch timestamps, type confusion, missing required fields, identity/digest/protocol/scope invariants, canonical lowercase digests and normalized protocol lists, preservation of legacy pre-relay records without `scope`, non-destructive failure on invalid authority, and rejection of invalid issuance time inputs before persistence. It also verifies default-clock rejection before secret generation, derived-expiry overflow rejection, non-destructive revoke failure under a pre-epoch clock, Unix epoch `0.0` compatibility, and that records emitted by `issue()` satisfy the stricter reader contract and reload successfully.

`tests/test_ingest_api_validation.py` unit-tests the endpoint clock/deadline validators, and `tests/test_ingest_credentials.py` covers the issuance endpoint boundary: an invalid effective clock or damaged persisted deadline fails closed with the fixed 503 responses without rotating an existing credential, an expired (or exactly-reached) deadline is rejected with `409`, a valid clock still caps the TTL at the Session deadline, and the Unix epoch `0.0` clock remains valid.

This is one record-level slice of the broader persistent-state hardening tracked in #87. Session, entitlement, and node authority validation remain separate concerns.
