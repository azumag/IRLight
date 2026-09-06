# Authentication authority validation

IRLight treats `users.json` and `auth_sessions.json` as authority, not as a cache that can be silently repaired from guesses. If either file exists but is malformed, authentication must fail closed and operators must preserve the original state for recovery.

## Current invariants

The authentication store accepts strict JSON only. Non-standard numeric constants such as `NaN`, `Infinity`, and `-Infinity` are rejected while reading, and writers use `allow_nan=False` so they cannot publish those values accidentally.

Persisted authority JSON is also decoded with duplicate object-key rejection. Python's default JSON decoder otherwise silently keeps the last duplicate key, which can make the bytes inspected during recovery differ from the object the service validates. IRLight rejects duplicate keys recursively instead of guessing which value was intended. The shared rule is used by the Control Plane's persisted authority readers, not request/response JSON parsing.

User records require their persisted identity, normalized email, password hash, role, status, and finite creation/update timestamps. Persisted emails must also match the registration writer's basic shape: they contain `@`, but do not begin or end with it. `updated_at` must not be earlier than `created_at`, matching the writer's monotonic record lifecycle; reversed timestamps are treated as damaged authority instead of being accepted silently. The record key must match the persisted user ID, and `email_index` must point back to the same user record. Optional display names must be strings when present.

Password hashes are accepted only in the exact format emitted by the current writer: `pbkdf2_sha256`, the configured 260,000-iteration work factor, a 16-byte salt, and a 32-byte SHA-256 digest. A damaged or restored record cannot supply a larger iteration count and turn login into an unbounded PBKDF2 job. Changing hash parameters requires an explicit migration; authentication does not infer or execute arbitrary persisted work factors.

Authentication session records also require their map key to be the exact lowercase 64-hex-character SHA-256 token digest emitted by the current writer. Arbitrary, truncated, oversized, non-hex, or uppercase keys are treated as damaged authority and rejected instead of being carried forward by GC or later writes.

Authentication-session records require a non-empty user ID and a CSRF token in the exact URL-safe 32-character form emitted by `secrets.token_urlsafe(24)`, plus finite numeric `created_at` and `expires_at` values. Truncated, oversized, padded, non-URL-safe, or non-ASCII CSRF tokens are rejected as damaged authority rather than being compared against request cookies or headers. Boolean, string, null, and non-finite timestamp values are invalid authority rather than values to coerce. A session is expired when `expires_at <= now`.

Invalid authority is not rewritten with defaults by a read path. Serialization also fails before replacing the existing authority file if the new payload cannot be represented as strict JSON.

## API failure contract

Authentication authority failures are service-availability failures, not bad credentials or invalid user input. `/v1/auth` endpoints and the shared `require_user` / `require_csrf` dependencies translate `AuthStateError` into HTTP 503 with the stable public detail `{"code":"AUTH_STATE_UNAVAILABLE"}`.

The public response deliberately does not include the authority path or the underlying exception text. In particular, a corrupt `users.json` must not be exposed as a registration 422 simply because `AuthStateError` inherits from `AuthError`, and a session-store write failure during login/logout must not escape as an uncontrolled 500.

## Recovery safety

Do not work around an authority error by deleting an `.initialized` marker, replacing a damaged file with `{}`, or deleting the state volume. Those actions can turn a detectable corruption or partial restore into silent authority loss. Preserve the state files and their initialization markers together and follow the explicit recovery procedure tracked in #90.

## Scope

This document describes the authentication-store slice implemented for #87. Other persisted authority stores still need the same record-level audit before #87 is complete, including explicit schema/enum decisions where compatibility policy is required. This change intentionally does not invent new user-status or role values and does not migrate damaged records automatically.
