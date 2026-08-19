# Ingest format / bitrate policy

## Scope

This slice enforces the first Phase B ingest policy after MediaMTX has accepted
an authenticated publisher and discovered its media tracks.

Node Agent polls the internal MediaMTX Control API (`/v3/paths/list`) on each
heartbeat. MediaMTX 1.20 exposes `tracks2`, `inboundBytes` and the current source
ID/type for each path. The Node Agent evaluates those values and, when a hard
policy violation is detected, uses the corresponding MediaMTX `kick` endpoint
to terminate the RTMP / RTMPS / SRT publisher.

The MediaMTX API remains internal-only; it is not published by the public node
compose overlay.

## Current policy

Hard rejection:

- video codec is not H.264
- audio codec is not MPEG-4 Audio (AAC)
- video resolution is not `1280x720` or `1920x1080`
- audio has more than two channels
- aggregate ingest bitrate stays above 6 Mbps for two consecutive samples

Warning only:

- AAC sample rate is not 48 kHz
- track dimensions / audio properties are unavailable
- first bitrate sample is not yet available

The bitrate limit can be changed with `NODE_INGEST_MAX_BITRATE_BPS`. The number
of consecutive over-limit samples is controlled by
`NODE_INGEST_BITRATE_VIOLATION_SAMPLES` (default: 2) to avoid disconnecting a
publisher because of a single short spike.

## Policy states

- `OFFLINE`: no publisher is online
- `UNKNOWN`: MediaMTX API could not be queried
- `PENDING`: tracks are valid but a bitrate sample is not available yet
- `ACCEPTED`: current track set and sampled bitrate are within policy
- `WARNING`: accepted with a non-fatal compatibility warning
- `REJECTED`: a hard violation was detected; Node Agent attempts to kick source

The latest observation is included in Node Agent heartbeat state. Significant
changes are retained as bounded node events (`ingest.format_detected`,
`ingest.policy_changed`, `ingest.rejected`, `ingest.disconnected`).

## Enforcement

MediaMTX source types map to these internal API endpoints:

- RTMP: `POST /v3/rtmpconns/kick/{id}`
- RTMPS: `POST /v3/rtmpsconns/kick/{id}`
- SRT: `POST /v3/srtconns/kick/{id}`

No credential secret is included in observations or events.

## Docker smoke proof

`scripts/smoke-compose.sh` includes a Node Agent in fake-supervisor mode while
it observes the real MediaMTX container.

The smoke test performs both:

1. authenticated H.264/AAC `640x360` publish -> `ingest.rejected`, publisher is kicked
2. authenticated H.264/AAC `1280x720` publish -> `ACCEPTED`, continuity reaches LIVE

The remainder of the existing cut / recovery / mute-persistence smoke scenario
then runs unchanged.

## Not covered by this slice

MediaMTX path metadata does not expose everything needed for the full Issue #3
policy. These remain follow-ups:

- actual FPS measurement
- GOP / keyframe interval measurement
- timestamp progression / rollback detection
- video-only / audio-only timeout over time beyond initial track validation
- per-protocol network quality metrics (for example SRT RTT / loss)
- user-facing Session event linkage after the on-demand Node bootstrap is tied
  to the actual Control Plane Session assignment

FPS / GOP / timestamp checks should be measured from the live media path rather
than inferred from configuration metadata.
