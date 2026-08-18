# RTMPS ingest deployment

## Purpose

Issue #3 requires authenticated RTMP, RTMPS and SRT ingest. The credential
authentication added in PR #32 is shared by RTMP and RTMPS: MediaMTX reports
both as authentication protocol `rtmp`. This document covers the TLS listener,
certificate delivery and rotation boundary for RTMPS.

## MediaMTX configuration

MediaMTX supports three RTMP encryption modes: `no`, `strict` and `optional`.
IRLight uses `optional` on Media Nodes so compatibility RTMP remains on TCP/1935
while RTMPS is available on TCP/1936.

The production overlay `docker-compose.node.rtmps.yml` sets:

```text
MTX_RTMPENCRYPTION=optional
MTX_RTMPSADDRESS=:1936
MTX_RTMPSERVERKEY=/run/secrets/rtmps_server_key
MTX_RTMPSERVERCERT=/run/secrets/rtmps_server_cert
```

The key and certificate are Compose secrets sourced from host files configured
through:

```text
NODE_RTMPS_KEY_FILE=/etc/irlight/tls/ingest.example.com.key
NODE_RTMPS_CERT_FILE=/etc/irlight/tls/ingest.example.com.fullchain.pem
```

Do not commit these files and do not bake them into the MediaMTX image.

## Stable DNS name

RTMPS clients validate the certificate against the hostname they connect to.
On-demand ConoHa nodes can receive a different public IPv4 address each time,
so Control Plane connection information must prefer a stable DNS name:

```text
IRLIGHT_INGEST_PUBLIC_HOST=ingest.example.com
IRLIGHT_INGEST_RTMPS_ENABLED=1
IRLIGHT_INGEST_RTMPS_PORT=1936
```

The certificate SAN must contain the exact hostname returned by
`IRLIGHT_INGEST_PUBLIC_HOST`. The DNS record may be repointed to the currently
prepared Media Node without changing the client-facing connection information.

## Starting a node with RTMPS

```bash
export NODE_RTMPS_KEY_FILE=/etc/irlight/tls/ingest.example.com.key
export NODE_RTMPS_CERT_FILE=/etc/irlight/tls/ingest.example.com.fullchain.pem

docker compose \
  -f docker-compose.node.yml \
  -f docker-compose.node.public.yml \
  -f docker-compose.node.rtmps.yml \
  up -d
```

The public overlay exposes:

- TCP/1935: RTMP
- TCP/1936: RTMPS
- UDP/8890: SRT

MediaMTX API, metrics, HLS and internal RTSP remain unexposed.

## Connection information

An RTMP-enabled ingest credential is also valid for RTMPS. When
`IRLIGHT_INGEST_RTMPS_ENABLED=1`, the issue API returns both endpoints with the
same one-time username/password credential:

```text
RTMP:  rtmp://ingest.example.com:1935/live/input
RTMPS: rtmps://ingest.example.com:1936/live/input
```

A credential issued with only `protocols=["srt"]` does not advertise RTMP or
RTMPS endpoints.

## Certificate rotation

Phase B uses one short-lived Media Node per Session, with a hard maximum runtime
far shorter than normal public certificate lifetimes. Rotation therefore uses
an operational boundary rather than hot-reloading the certificate inside an
active stream:

1. Renew the certificate on the deployment/control host.
2. Write the new key and full chain to new files with restrictive permissions.
3. Validate that the key matches the certificate and that the certificate SAN
   contains `IRLIGHT_INGEST_PUBLIC_HOST`.
4. Atomically replace the host paths used by `NODE_RTMPS_KEY_FILE` and
   `NODE_RTMPS_CERT_FILE`.
5. New Media Nodes start with the new certificate automatically.
6. Do not restart a MediaMTX process carrying an active Session only to rotate a
   certificate. Existing short-lived nodes can finish normally unless emergency
   revocation requires otherwise.
7. After the old certificate's last possible Session lifetime has elapsed,
   remove the superseded key material.

For emergency key compromise, drain/stop affected Sessions and recreate their
Media Nodes with the replacement certificate. That disruptive path belongs in
the operations/security runbook.

## Verification

`scripts/smoke-rtmps.sh` creates an ephemeral self-signed certificate whose SAN
is `mediamtx`, enables MediaMTX RTMPS, adds that certificate to the Control
Plane test trust store and executes the existing Destination verifier against:

```text
rtmps://mediamtx:1936/live/input
```

The verifier performs normal hostname/certificate validation and then the RTMP
v3 transport handshake. CI requires the result to be `VERIFIED` with protocol
`rtmps` and peer port `1936`.

The authenticated publish boundary itself remains the same MediaMTX HTTP auth
flow already exercised by the RTMP/SRT Docker smoke test; MediaMTX labels an
RTMPS publish as protocol `rtmp` when calling the authentication server.

## Remaining Issue #3 work

RTMPS TLS termination and deployment/rotation are covered by this slice. The
remaining ingest work is primarily:

- media format and policy inspection (H.264/AAC, resolution, fps, bitrate, GOP);
- connected/disconnected/rejected event propagation;
- authentication rate limiting and abuse controls;
- short-lived node-local authorization cache for Control Plane outages;
- OBS, smartphone and hardware encoder compatibility tests.
