# Control Plane state readiness

IRLight exposes two different process-health signals:

- `GET /healthz` is liveness only. It answers while the API process is running.
- `GET /readyz` is readiness for the local authority that the Control Plane initializes at startup.

A successful readiness response is:

```json
{"status":"ready"}
```

If required authority cannot be inspected safely, `/readyz` returns HTTP 503 with only the fixed public reason code `STATE_AUTHORITY_UNAVAILABLE`. The response does not include a state path, raw JSON, secret, parser error, or validation detail.

## What readiness checks

The current check covers the authority that `app.py` initializes synchronously:

- `control.json`
- `catalog.json`
- `users.json`
- `auth_sessions.json`
- Node `nodes.json` in `NODE_STATE_DIR` (or `STATE_DIR` when unset)
- the legacy bootstrap-token rollback fuse when that optional file/marker exists

For each canonical authority, both the JSON file and its durable initialization marker must exist as regular files and the payload must pass the same structural/security validation used by the owning store. A missing file after initialization, malformed JSON, malformed marker, unsafe persisted Destination URL, invalid auth record, or invalid Node/token authority therefore makes the process non-ready.

Session, entitlement, Destination-secret, ingest, and other lazily initialized stores are intentionally not made deployment-readiness dependencies by this change. Their existing request paths remain fail-closed. Promote one of those stores into `/readyz` only when its deployment lifecycle is explicitly mandatory; otherwise a brand-new environment could be reported unavailable merely because an optional/lazy feature has never been used.

## Read-only contract

Readiness does not call normal store lock/getter methods because those code paths may create directories, lock files, initialization markers, or first-run JSON. The inspector only opens existing regular files read-only and validates their contents. It does not:

- create missing state or markers;
- create lock files;
- repair or migrate authority;
- delete provider resources;
- clear corrupt data;
- remove initialization fuses.

The configured state roots are opened without following symlinks and pinned by file descriptor for the duration of the inspection. Each authority file and initialization marker is likewise opened without following symlinks and remains pinned through payload validation. Immediately before accepting a check, readiness verifies that the pathname still names the same regular-file inode. If a normal atomic writer replaces an authority or marker while inspection is in progress, the check fails closed instead of reporting a stale pre-replacement snapshot as ready. No store lock is created or acquired for this purpose, so a concurrent writer can cause a transient non-ready result rather than being blocked indefinitely.

The legacy bootstrap-token ledger predates mandatory initialization markers, so a valid ledger without a marker remains compatible. That marker absence is still part of the inspected snapshot: if a writer creates the marker while the legacy ledger is being validated, readiness fails closed rather than reporting `OK` from the pre-initialization view. Once a marker already exists, it is pinned and identity-checked like the canonical authority markers.

This property is covered by tests that compare file contents, mtimes, and the file set before and after a readiness check, together with deterministic replacement-race tests for authority files, initialization markers, and the legacy marker-appearance transition.

## Deployment use

Keep the container/process liveness probe on `/healthz`. Use `/readyz` for traffic readiness and alerts that should stop new work when authoritative state is unavailable. Do not convert a `/readyz` failure into an automatic volume reset or `docker compose down -v` action.

A 503 means an operator should inspect the mounted state and initialization markers from a controlled maintenance context. Preserve the affected files before recovery. Writers, including a separate reaper, should be quiesced before restoring authority.

## Administrative inspection

The Control Plane image also includes a read-only inspection command for a controlled maintenance shell:

```bash
python /app/state_inspect_cli.py
```

It uses `STATE_DIR` and `NODE_STATE_DIR` by default. Explicit directories can be supplied with `--state-dir` and `--node-state-dir` when inspecting a mounted copy. The command prints one JSON object and exits `0` only when every startup authority is readable and valid; otherwise it exits `2`.

The output contains only stable authority labels (`control`, `catalog`, `users`, `auth_sessions`, `nodes`, `legacy_bootstrap_tokens`), `OK` / `UNAVAILABLE`, and normalized reasons such as invalid JSON or failed validation. It never prints state paths, raw records, credentials, parser exception details, or validator exception details. The command uses the same non-mutating file reader as `/readyz`, so it does not create a missing lock, marker, directory, or authority file.

Example shape:

```json
{"checks":[{"authority":"control","reason":null,"status":"OK"},{"authority":"catalog","reason":"required state contains invalid JSON","status":"UNAVAILABLE"}],"status":"UNAVAILABLE"}
```

Use this only to identify which local authority requires investigation. It deliberately has no repair, restore, marker deletion, provider cleanup, or credential-reset option.

A backup restore can be compared against a protected reference without changing either tree by following [`state-restore-drill.md`](state-restore-drill.md). The comparison is intentionally limited to startup authority and does not authorize production restore or provider reconciliation.

## Recovery boundary

Readiness and `state_inspect_cli.py` are detection, not automatic repair. Issue #90 also tracks the broader recovery procedure: backup generation/fencing, provider inventory reconciliation, credential re-issuance after restoring older state, and explicit restore/reconcile operations. Those steps can change security or provider ownership semantics and must not be guessed by the readiness handler or inspection command.
