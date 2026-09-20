# Ingest quality / DEGRADED model

## Purpose

Hard format policy and live quality are intentionally separate.

- `REJECTED`: unsupported codec/resolution/channel count or sustained bitrate above the maximum. The publisher is kicked.
- `DEGRADED`: the stream is authenticated and structurally supported, but the live media is unhealthy. The publisher remains connected while the condition is surfaced to Control Plane.

This lets IRLight keep a recoverable stream alive instead of turning every quality dip into a disconnect.

## Sampling

The Node Agent uses MediaMTX's internal path API for codec/resolution/byte counters and samples the internal RTSP path with `ffprobe`.

Default sample window: 4 seconds.

The sampler records:

- video/audio frame counts
- video FPS from timestamp span
- video/audio timestamp progression
- timestamp regressions
- keyframe count and maximum observed GOP gap
- aggregate ingest bitrate from MediaMTX byte counters

MediaMTX API, RTSP and metrics remain internal-only on production nodes.

When `NODE_INGEST_SAMPLE_URL_FILE` is configured, the authenticated sample URL is read at sample time through the Node Agent runtime secret-file boundary. The resolved target must be a regular file, reads are bounded to 64 KiB, FIFO/device inputs are rejected before content reads, and projected-secret symlinks to regular files remain supported. An unreadable, oversized, invalid UTF-8, replaced, or mutated configured file fails closed as `MEDIA_SAMPLE_FAILED`; the sampler does not silently fall back to `NODE_INGEST_SAMPLE_URL`. The protected URL continues to be passed to ffprobe through stdin rather than argv.

## DEGRADED reasons

- `VIDEO_TIMEOUT`: no meaningful video progress observed in the sample; this includes zero frames and a negligible residual burst across an otherwise full sample window
- `AUDIO_TIMEOUT`: no meaningful audio progress observed; this includes zero frames and a negligible residual burst across an otherwise full sample window
- `VIDEO_TIMESTAMP_REGRESSION`
- `AUDIO_TIMESTAMP_REGRESSION`
- `VIDEO_TIMESTAMP_STALLED`
- `AUDIO_TIMESTAMP_STALLED`
- `FPS_OUT_OF_RANGE`
- `GOP_TOO_LONG`
- `KEYFRAME_TIMEOUT`
- `BITRATE_TOO_LOW`
- `MEDIA_SAMPLE_TIMEOUT` / `MEDIA_SAMPLE_FAILED` when an online source cannot be sampled

`VIDEO_TIMESTAMP_STALLED` / `AUDIO_TIMESTAMP_STALLED` remain the classification for media that makes some meaningful timestamp progress but fails the configured minimum-progress ratio. Only near-zero residual progress after the sampler has consumed essentially the full sample window is promoted to `*_TIMEOUT`.

Warnings that do not by themselves make the source DEGRADED include `FPS_NON_PREFERRED`, `GOP_UNOBSERVED`, and the first low-bitrate sample before the consecutive-sample threshold is reached.

## Defaults

| Check | Default |
| --- | --- |
| sample window | 4 s |
| hard FPS range | 20–40 fps |
| preferred FPS | 24–35 fps |
| max GOP | 4 s |
| minimum timestamp progress | 60% of sample window |
| minimum bitrate | 500 kbps |
| low-bitrate confirmation | 2 consecutive samples |

All values are environment-configurable; beta data should be used to tune them.

## Events

Control Plane node state stores the latest quality snapshot and emits bounded events:

- `ingest.format_detected`
- `ingest.degraded`
- `ingest.recovered`
- `ingest.disconnected`
- `ingest.rejected`

The current Phase B bootstrap is not yet the final user-session assignment model, so these are Node events. Mapping them into canonical user Session events belongs with the #8 assignment integration.

## Smoke-test diagnostic boundary

`scripts/smoke-ingest-quality.sh` creates a real login/session, obtains an ingest credential, and embeds that credential in the disposable RTMP publisher URL. The publisher output is stored under a run-local `umask 077` temporary directory. Because service logs and Node API payloads can also become credential-bearing if auth/logging behavior regresses, failure handling does not replay raw `node-agent`, `mediamtx`, `control-ui`, publisher, or Node API diagnostics into CI. It emits fixed context only and keeps the existing quality assertions intact. This is intentionally stricter than maintaining a partial redaction vocabulary.

The smoke still verifies that a valid 10 fps stream remains connected while surfacing `DEGRADED/FPS_OUT_OF_RANGE`, then becomes `OFFLINE` after publisher exit, and that a fresh 30 fps publisher reaches `ACCEPTED`.

## Non-goals of this slice

- automatic transition of the Continuity Engine itself into `DEGRADED` / `HOLDING`
- adaptive bitrate or transcoding
- platform-specific recommendations
- full device compatibility certification
- long-window QoE scoring
