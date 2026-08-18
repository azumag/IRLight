# Ingest credential authentication

## Scope

This slice implements the first authenticated ingest boundary for Issue #3.
It covers credential issuance/rotation/revocation and MediaMTX publish
authorization for RTMP and SRT. RTMPS certificate termination and media-format
policy enforcement are intentionally left for the next slice.

## Credential model

A Session that reached `READY_WAIT_INGEST`, `LIVE` or `HOLDING` can issue one
active ingest credential:

- username: Session UUID
- secret: 32-byte random value encoded with URL-safe base64
- supported protocols: RTMP and/or SRT
- expiration: at most 12 hours and never beyond the Session absolute deadline
- rotation: issuing a replacement immediately revokes the prior credential
- stop: stopping the Session revokes its active credential

Only `SHA-256(secret)` is persisted in `STATE_DIR/ingest_credentials.json`.
The raw secret is returned once by the issue API and cannot be recovered later.

## User API

Issue / rotate a credential:

```http
POST /v1/sessions/{session_id}/ingest-credentials
X-CSRF-Token: ...
Content-Type: application/json

{
  "protocols": ["rtmp", "srt"],
  "ttl_seconds": 3600
}
```

The response contains the raw `credential_secret` once, together with protocol
connection information.

Retrieve non-secret connection metadata later:

```http
GET /v1/sessions/{session_id}/connection-info
```

The password / full authenticated SRT URL are intentionally not recoverable
from this endpoint. Rotate the credential when the user needs the secret again.

Revoke explicitly:

```http
DELETE /v1/sessions/{session_id}/ingest-credentials/{credential_id}
X-CSRF-Token: ...
```

## MediaMTX authentication

MediaMTX uses external HTTP authentication:

```yaml
authMethod: http
authHTTPAddress: http://control-ui:8080/internal/ingest/auth
```

Production nodes override `authHTTPAddress` with `NODE_INGEST_AUTH_URL` /
`MTX_AUTHHTTPADDRESS` so the Media Node can reach the Control Plane.

Only publish to `live/input` is authenticated. Internal reads and the
`output/relay` publisher used by the Continuity Engine are excluded.

The Control Plane accepts a publish only when:

1. `action == publish`;
2. `path == live/input`;
3. protocol is RTMP or SRT;
4. the username resolves to a non-terminal Session that is ready for ingest;
5. the supplied secret digest matches the active, unexpired credential;
6. the credential allows the requested protocol.

Authentication failures deliberately use the same generic response for an
unknown Session, wrong secret, expired secret and stopped Session.

## RTMP client form

MediaMTX carries RTMP credentials through `user` / `pass`. The API returns:

```text
Server URL: rtmp://<node>:1935/live/input
Username:   <session UUID>
Password:   <credential secret>
```

OBS users can enable the authentication fields in a Custom streaming service.
Clients that support query authentication can send the same username/password
using MediaMTX's `user` / `pass` query parameters.

## SRT client form

The API returns an SRT URL using MediaMTX's authenticated streamid format:

```text
srt://<node>:8890?streamid=publish:live/input:<session UUID>:<credential secret>
```

The public node compose overlay exposes UDP/8890 in addition to RTMP/1935.

## Concurrent publishers

`live/input` sets `overridePublisher: false`. A second publisher cannot evict an
already active publisher merely by possessing the same credential. After the
old publisher disconnects, the same credential can reconnect until it expires
or is revoked.

This is the initial single-input policy. Connection conflict events and richer
stale-connection reconciliation remain follow-up work.

## Security boundary

`/internal/ingest/auth` is an internal Media Node -> Control Plane endpoint. It
must not be exposed as an unrestricted public API. Production deployment still
needs the final machine-network protection (private network, mTLS or equivalent)
before public beta.

The following are intentionally not included yet:

- RTMPS listener certificate provisioning / rotation;
- ingest auth attempt rate limiting and account/IP lockout;
- H.264/AAC, resolution, fps, bitrate and GOP enforcement;
- `ingest.connected` / `ingest.disconnected` audit event propagation;
- node-local auth cache for temporary Control Plane outages.
