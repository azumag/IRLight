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

The collector validates its effective clock value before acquiring the
authentication-state lock or reading authority. Both an explicitly supplied
`now` and the default system clock must normalize to a finite runtime number;
`NaN`, infinity, and integer values too large for finite float normalization are
rejected without touching `auth_sessions.json`.

The collector validates the complete authority before deleting anything.
Malformed, missing-after-initialization, non-finite, or otherwise invalid state
fails closed and is not rewritten. A run with no records to delete also avoids
an authority write.

## Scheduling

The existing `deploy/systemd/irlight-reaper.service` already invokes
`/app/reaper_cli.py` inside the running Control Plane container. The reaper CLI
therefore also runs the bounded authentication-session GC on each normal reaper
sweep, using the same mounted `STATE_DIR` as the API. No additional scheduler,
container, datastore, or provider resource is required.

The ordinary Session/provider reaper runs first. Authentication-session cleanup
runs afterwards so damaged authentication authority cannot suppress cleanup of
stale or orphaned provider resources. A successful sweep adds only aggregate
`auth_session_gc_*` counters to the existing flat reaper result. If auth-session
GC cannot safely inspect or update its authority, the reaper keeps the provider
cleanup result, reports the fixed `AUTH_SESSION_GC_FAILED` reason, and exits
non-zero without printing the authority path or record contents.

The periodic default remains 1,000 expired authentication sessions per sweep.
It can be changed explicitly when invoking the reaper:

```bash
python /app/reaper_cli.py --auth-session-gc-max-delete 1000
```

The accepted range is the same as the standalone collector: 1 through 10,000.
Only records whose `expires_at <= now` are removed, so an active authentication
Session is not selected by the GC. Session validity itself does not depend on
GC frequency because expired Sessions are already rejected by request-time
authentication.

Do not point the reaper at a copied or guessed state directory, and do not work
around an authority error by deleting initialization markers or creating an
empty `auth_sessions.json`.

## Remaining admission-control work

Expired-record retention and its periodic execution are handled here.
PBKDF2-heavy registration/login also has a bounded multi-worker concurrency
gate; see [auth-kdf-admission.md](auth-kdf-admission.md). Issue #86 still tracks
policy that this repository should not infer without deployment evidence:
source IP / normalized-email rate limits, trusted-proxy handling, cluster-wide
quota across independent replicas, and any per-user active auth Session cap.
