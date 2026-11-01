# Relay reader limit (Issue #76)

`RELAY_ONLY` Sessions expose an authenticated pull endpoint that external
clients (for example OBS) read with a per-Session relay credential. Issue #76
requires the number of relay clients to be bounded, with one reader per
Session as the MVP default.

The node-local ingest auth proxy now enforces that bound for external RTMP
reads of the relay path. The proxy asks the MediaMTX control API for the
current readers of the relay path before forwarding the authorization
upstream. It counts MediaMTX reader types `rtmpConn` and `rtmpsConn` as
external relay clients; internal protocol readers such as `rtspSession` and
HLS preview readers do not consume the external-client slot. A reader whose
type is missing or malformed is counted conservatively so an unexpected API
shape cannot silently bypass the bound.

When the observed external reader count reaches `NODE_RELAY_MAX_READERS`
(default `1`, accepted range `1`-`32`), the request is denied with HTTP 403
`relay reader limit reached` and **never reaches the Control Plane authorize
endpoint**, so saturated reads cannot flood the upstream.

## Semantics

- Only `read` + `rtmp` authorization requests to `NODE_RELAY_PATH` (default
  `output/relay`) are limited. MediaMTX uses the RTMP auth protocol value for
  the external RTMP/RTMPS read boundary, while its path API distinguishes the
  active reader as `rtmpConn` or `rtmpsConn`.
- Publishes, other paths, and SRT/RTSP authorization requests are unaffected.
  Internal `rtspSession` readers and HLS preview readers are explicitly
  excluded from the external relay-client count.
- The reader count is fresh MediaMTX state, so a dropped TCP connection frees
  its slot immediately — OBS reconnects are not penalized by stale counts.
- An `RELAY_SOURCE_OFFLINE` observation means the path currently has no
  readers; the request falls through to normal upstream authorization.
- An observation that cannot be trusted (MediaMTX API error, exception,
  `UNKNOWN` status, unexpected reason code, missing or malformed
  `reader_count`) fails closed with HTTP 503
  `relay reader limiter unavailable` and `Retry-After: 2`. A bound that
  cannot be measured is never silently bypassed.
- The check runs before the upstream round-trip, so the denial also bounds
  Control Plane work during a connection flood.

## Boundaries

- The bound is best-effort against simultaneous admits racing a single
  observation; a strict media-plane reader bound is follow-up work.
- The cap limits concurrent external RTMP/RTMPS readers, not brute-force
  attempts; credential guessing remains governed by the Control Plane ingest
  auth guard.
- `NODE_RELAY_PATH` must stay aligned with the relay path configured for the
  MediaMTX/auth boundary; `docker-compose.node.yml` passes both env values
  through.

Generated with ChatGPT
