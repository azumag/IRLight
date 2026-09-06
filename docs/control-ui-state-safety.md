# Control UI state freshness and fail-closed behavior

The Phase 0 Control UI polls `/api/status` once per second and treats the response as an observation, not as durable UI truth. The runtime timestamp is accepted only while it is within the existing three-second freshness window. When that timestamp is stale, runtime-derived session/video/input/actual/version fields are replaced with `状態確認不能`, while the freshly read desired control state may still be shown. Audio actions remain disabled while the runtime is stale, the actual audio mode is unknown, or the latest control command has not been acknowledged.

If a status poll fails, the UI must not continue presenting values from the last successful response as though they were current. Session, video source, input presence, desired/actual audio state, control/runtime version, and last-update fields are replaced with `状態確認不能`; the audio action is disabled and shown with the neutral unavailable style. A later successful poll repopulates the fields from the newly fetched snapshot.

A failed `PUT /api/audio` is also treated as an unknown outcome. The UI closes actions first, re-reads `/api/status`, and only re-enables control after a fresh authoritative snapshot satisfies the same freshness and command-acknowledgement checks. It does not retry the toggle with a new idempotency key.

This remains a Phase 0 local-only surface. It does not replace the Session-authenticated control API or change the deployment/security boundary described in `AGENTS.md`.
