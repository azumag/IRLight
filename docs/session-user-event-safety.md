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

The same endpoint also has a **64 KiB raw request-body limit** in ASGI middleware,
before FastAPI/Pydantic parses the JSON body. `Content-Length` is rejected early
when it exceeds the limit, and requests without a trustworthy length are counted
while their ASGI body chunks are received. An oversized request returns HTTP 413
with fixed code `USER_EVENT_REQUEST_TOO_LARGE`, without echoing request data or
revealing whether the Session exists. The 64 KiB transport budget is deliberately
larger than the 8 KiB persisted-payload budget so normal escaped JSON and the
request envelope remain compatible while whitespace/duplicate-key inflation is
bounded. A reverse proxy may impose an equal or stricter global body limit as a
defense-in-depth measure.

User events and internal audit events still share the retained event ring today;
separating their retention and reserving the internal event namespace remain
follow-up work under Issue #93.
