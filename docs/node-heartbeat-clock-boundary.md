# Node heartbeat clock boundary

Node heartbeat processing keeps two timestamp domains distinct.

- `ingest.observed_at`, `egress.observed_at`, and `relay_client.observed_at` are Node Agent observations. They must be finite and non-negative at the request boundary. Unix epoch `0.0` remains valid.
- The Control Plane samples its wall clock once per accepted heartbeat transaction, after Node authentication and before mutating Node or Session state. That validated timestamp is used for `last_heartbeat_at`, Node event `occurred_at`, and Session ingest / egress event timestamps where those stores already accept an explicit event time.
- Agent-provided `observed_at` values remain in observation payloads and are not rewritten to the Control Plane timestamp.

The single Control Plane sample prevents a second wall-clock read from becoming invalid after Session heartbeat state has already been updated but before `nodes.json` is published. An invalid Control Plane clock fails closed before heartbeat mutation. Negative Agent observation timestamps are rejected by request validation before the handler acquires Node state for mutation.

This contract intentionally does **not** introduce a future-skew ceiling, cross-authority timestamp ordering, or a distributed transaction between `nodes.json` and Session authority. Relay Session aggregation currently retains its existing SessionStore clock semantics; this change only pins the Node heartbeat transaction and the event paths that already support an explicit timestamp without changing lifecycle behavior.
