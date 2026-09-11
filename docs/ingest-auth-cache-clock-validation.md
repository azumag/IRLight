# Ingest auth cache clock validation

The Node-local ingest positive cache is a bounded continuity fallback, not a second authentication authority. Its expiry decisions therefore depend on trustworthy timestamps: if the Node cannot determine whether a cached grant is still inside its stale-allow window, it must not authorize from that cache.

`PositiveAuthCache` validates the effective local clock before lookup, pruning, size inspection, or mutation. It accepts only finite, non-negative numeric values. Booleans, negative values, NaN, positive or negative infinity, non-numeric values, and integers that cannot be represented as a float are rejected with a fixed `RuntimeError`.

The Control Plane supplied `cache_valid_until` receives the same finite, non-negative validation before a successful authorization is inserted into the Node cache. This prevents a malformed or non-finite validity timestamp from creating a grant whose lifetime cannot be bounded.

## Failure behavior

A successful live Control Plane authorization remains authoritative for the current request even if the Node clock or `cache_valid_until` is invalid. In that case the proxy returns the Control Plane response but deliberately does not prime the positive cache. The cache is only a future outage fallback, so rejecting the current request would not improve the authentication decision.

When the Control Plane is unavailable or returns a server-side failure, cached authorization is permitted only after the local clock has been validated. If the clock is invalid, cache age cannot be proven and the proxy returns the existing fail-closed `503` response with `Retry-After: 2`; it never turns an unmeasurable cached entry into an allow decision.

Validation happens before cache pruning or entry mutation so a rejected timestamp cannot silently reshape the cache. Raw usernames, passwords, and tokens remain outside the cache entries; only the existing canonical request digest and expiry timestamp are retained.

## Unchanged behavior

This hardening does not change the cache key, the local maximum age or entry count, explicit 4xx eviction, relay-read no-cache policy, upstream timeout, or MediaMTX authentication response contract. With valid timestamps, existing publisher reconnect behavior is unchanged.

Regression coverage is in `tests/test_ingest_auth_cache_clock.py` and checks explicit and default invalid clocks, invalid upstream validity timestamps, preservation of a live authoritative success without cache priming, and fail-closed outage fallback.
