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

User-authored event types also cannot use the trusted internal namespaces
`session.*`, `ingest.*`, `egress.*`, `relay.*`, or `media_control.*`. The check
is case-insensitive and ignores surrounding whitespace, so a cosmetic spelling
variant cannot impersonate a trusted lifecycle/media fact for a downstream
consumer that normalizes event names. Rejection is HTTP 422 with fixed code
`USER_EVENT_TYPE_RESERVED`. Other custom user event names remain accepted for
compatibility; this change does not yet narrow the endpoint to only `user.note`.

## Retention separation

Retained events live in one merged, sequence-ordered list per Session, but the
list is trimmed as two independent rings:

- internal (non-`user-api`) events keep the newest `SESSION_EVENT_LIMIT`
  (1000) entries — the same retention as before this change;
- `user-api` events keep the newest `SESSION_USER_EVENT_LIMIT` (200) entries.

Because each ring evicts only its own oldest entries, any volume of
user-authored posts can evict only older user notes and can never push
internal lifecycle/audit events out of the retained history. The bound also
caps one owner's worst-case persisted user-note footprint at roughly
200 × the 8 KiB payload budget.

Evicted user notes can leave mid-list `sequence` gaps in the merged view.
Consumers must not assume retained sequences are contiguous; `earliest_sequence`,
`latest_sequence`, `has_more`, and `retention_gap` still describe front
truncation and cursor progress as before. States persisted before the user
ring existed keep their already-retained events and are bounded on the next
append rather than rewritten at load.

Fully separate audit storage, an idempotency-key contract, and per-owner
posting-frequency limits remain follow-up work under Issue #93.
