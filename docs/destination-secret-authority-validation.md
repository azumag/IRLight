# Destination secret authority timestamp validation

`destination_secrets.json` is authority state for encrypted egress publish credentials. Its timestamp fields are therefore validated before the store trusts or mutates a record.

## Timestamp boundary

`created_at` and `updated_at` must be finite, non-negative Unix timestamps. Boolean values, strings, missing values, negative numbers, NaN, positive or negative infinity, and values that cannot be represented as a finite float are rejected with `DestinationSecretError`. Unix epoch `0.0` remains valid.

The same rule applies to both persisted records and the effective writer clock used by `DestinationSecretStore.put()`. An invalid explicit `now` value or default `time.time()` result is rejected before Fernet encryption, record mutation, or authority-file replacement. Existing authority bytes are not repaired, deleted, or replaced as part of that failure.

## Scope

This validation does not change Fernet encryption, master-key loading, `secret_ref` semantics, rotation policy, future-clock skew policy, or timestamp ordering such as `updated_at >= created_at`. Those remain separate policy decisions.

The store must not add plaintext secrets, ciphertext, or master-key material to validation errors or logs.
