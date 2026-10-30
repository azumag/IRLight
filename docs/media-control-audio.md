# Per-Session Media Control — audio (Issue #15, CP-side contract)

`PUT /v1/sessions/{session_id}/media-control/audio` is the final-state audio
command for one Session. It is a PUT of the desired final mode, never a toggle,
so API retries cannot accidentally invert the operator's intent.

This slice lands the Control Plane authority-side contract only: persisted
desired/actual state, command versioning, idempotency, conflict detection, and
the audit event. Media Node delivery of the desired mode and the
applied/failed acknowledgement are follow-up work under Issue #15.

## Request

- owner-authenticated Session access and CSRF, same boundary as the other
  mutating Session endpoints;
- `Idempotency-Key` header is required (200 chars maximum);
- JSON body `{"mode": "LIVE" | "MUTED", "expected_version": int >= 0}`;
  `expected_version` must equal the persisted command version so a stale UI
  cannot overwrite a newer command.

## Response

```json
{
  "desired_mode": "MUTED",
  "actual_mode": "APPLYING",
  "version": 1,
  "command_id": "4c7a...."
}
```

`desired_mode` is the last accepted command (`LIVE` or `MUTED`).
`actual_mode` is the last state confirmed applied by the Media Node
(`LIVE`, `MUTED`, `SILENT_FALLBACK`, `APPLYING`, or `FAILED`). Until Node delivery lands, any
accepted command whose desired mode differs from the confirmed actual mode is
reported as `APPLYING`; nothing claims the mute has already taken effect.

## Conflicts and rejections

- HTTP 409 `{"code": "VERSION_CONFLICT", "current_version": N, "current_mode": M}`
  when `expected_version` is stale; the response carries the current version so
  the caller can re-fetch and retry;
- HTTP 409 `{"code": "IDEMPOTENCY_CONFLICT"}` when the `Idempotency-Key` was
  already accepted for a different mode;
- HTTP 409 `{"code": "SESSION_NOT_RUNNING"}` for terminal Sessions
  (`STOPPED`, `FINISHED`, `FAILED`) — commands are accepted on active Sessions
  including `HOLDING` so mute survives input reconnects;
- HTTP 400 when `Idempotency-Key` is missing; HTTP 422 for a malformed body;
- HTTP 404 for a Session that does not exist or is not owned by the caller.

A replay of the exact same accepted command (same `Idempotency-Key` and mode)
returns the recorded response without bumping `version` or appending another
event.

## Persistence and audit

The state lives in the Session authority record under
`media_control.audio` (`desired_mode`, `actual_mode`, `version`, `command_id`,
`idempotency_key`, `updated_at`). Loading validates the shape, mode enums,
version, key length, and timestamp; malformed state fails closed like any other
Session record corruption. Legacy records without `media_control` are
backfilled with `LIVE`/`LIVE` version 0 on first access.

Every newly accepted command appends a `media_control.audio.requested`
internal event (`origin: "control-api"`, so the user-events endpoint can never
mint it) with `command_id`, `desired_mode`, `version`, and
`operator_user_id` in the payload — inside the same authority transaction as
the state change.

## Follow-up work (not in this slice)

- delivering `desired_mode` to the Media Node and recording
  `media_control.audio.applied` / `media_control.audio.failed` /
  `media_control.audio.reconciled` with reason codes;
- `SILENT_FALLBACK` actual reporting and pipeline-time measurements;
- the Web UI mute control reading `desired_mode`/`actual_mode`;
- silent-AAC switch quality verification on real outputs.
