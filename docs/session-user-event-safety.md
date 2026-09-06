# User-authored Session event safety

`POST /v1/sessions/{session_id}/events` accepts owner-authenticated, CSRF-protected
user-authored metadata. Because this data is persisted in the shared Session
authority, it has a stricter budget than internal events.

Before `SessionStore.append_event()` is called, the Control Plane now rejects a
payload that exceeds any of these persistence limits:

- serialized UTF-8 JSON payload: 8 KiB maximum;
- nested object/array depth: 8 maximum (the root object is depth 1);
- total object members plus array elements: 128 maximum;
- non-finite numbers and non-JSON runtime values are rejected.

The public failure is HTTP 422 with the fixed code
`USER_EVENT_PAYLOAD_INVALID`; the response does not echo payload contents or
internal serializer errors. Internal Session-event producers do not pass through
this user-facing budget.

These limits bound what reaches the JSON authority, but they do **not** yet bound
the raw HTTP body before FastAPI parses it. A proxy/ASGI request-body cap remains
required for pre-parse resource protection. Likewise, user events and internal
audit events still share the retained event ring today; separating their
retention and reserving the internal event namespace remain follow-up work under
Issue #93.
