# Authentication session garbage collection

Expired login sessions are rejected by the request path, but they still occupy
`auth_sessions.json` until explicitly removed. `auth_session_gc.py` provides a
bounded maintenance operation that uses the same inter-process lock, authority
validation, initialization fuse, and atomic writer as the authentication store.

## Inspect before deleting

Run inside the Control Plane environment with the same `STATE_DIR` as the API:

```bash
python3 apps/control-api/auth_session_gc.py --dry-run --max-delete 1000
```

The command prints counts only. It does not print token hashes, CSRF tokens,
user IDs, file paths, or raw authority records.

## Delete expired records

```bash
python3 apps/control-api/auth_session_gc.py --max-delete 1000
```

A run deletes at most 1,000 expired records by default. The hard per-run maximum
is 10,000. Expiration uses `expires_at <= now`, matching request authentication.
Oldest expired records are removed first with a stable token-hash tie breaker.
If more expired records remain, the JSON result reports `expired_remaining` and
a later maintenance run may continue cleanup.

The collector validates the complete authority before deleting anything.
Malformed, missing-after-initialization, non-finite, or otherwise invalid state
fails closed and is not rewritten. A run with no records to delete also avoids
an authority write.

## Scheduling

This change intentionally does not choose an infrastructure-specific scheduler.
Production may invoke the command from a systemd timer, cron job, or deployment
scheduler, but only one using the exact Control Plane `STATE_DIR`. Start with a
low frequency such as hourly; session expiry is already enforced on reads, so
GC frequency affects storage/I/O rather than authentication validity.

Do not run the collector against a copied or guessed state directory, and do
not work around an authority error by deleting initialization markers or
creating an empty `auth_sessions.json`.

## Remaining admission-control work

This collector addresses expired-record retention only. Issue #86 also tracks
rate limits and concurrency bounds around PBKDF2-heavy registration/login, plus
any policy for per-user active-session caps. Those require a shared admission
control design for multi-worker deployments and are not implicitly decided by
this maintenance command.
