# Node authority validation

`nodes.json` is authoritative for Node identity, bootstrap-token consumption, heartbeat liveness, and stop intent. Corrupt state must not be interpreted as an empty registry, a reusable bootstrap token, or a missing heartbeat.

## Load and write contract

The Control Plane rejects non-standard JSON numeric constants (`NaN`, `Infinity`, `-Infinity`) and writes Node/token authority with `allow_nan=False`. Validation runs on canonical reads and before replacement writes. A rejected in-memory update therefore does not replace the last readable authority file.

Canonical reads, startup reads of existing authority, and reads of an existing bootstrap-token fuse require the file to remain present through `open()`. A file disappearing after an existence preflight fails as `NodeStateError`, even for legacy files without an initialization marker; it never becomes an empty registry or ledger. Direct compatibility reads may return their supplied default only for an uninitialized missing file. Cold startup still creates the default authority explicitly when neither authority nor token fuse has ever existed. These checks do not claim to detect loss of an entire volume and its markers; the existing mount/generation readiness contract remains separate.

Each Node must keep a matching `node_id`, non-empty Session/provider/boot/agent identity, a valid access-token SHA-256 digest, known `status` and `desired_state`, finite lifecycle timestamps, and correctly typed safety booleans/counters. `next_node_seq` is a strict positive integer and must remain ahead of canonical `node-NNNN` IDs so corruption cannot overwrite an existing Node on the next bootstrap. Present ingest, egress, relay-client observations and Node events are structurally checked, while every nested numeric value must be finite.

Node `status`, `desired_state`, present `egress_mode`, and observation `status`
must be strings before they are checked against their existing enum vocabulary.
JSON arrays and objects fail as `NodeStateError` rather than escaping validation
as an unhashable-value `TypeError`. Rejected reads leave authority and markers
unchanged, and rejected writes stop before atomic publication. The Reaper treats
these malformed enum values like other untrusted Node registry data and skips
heartbeat enforcement for that sweep; it does not infer that the Nodes vanished.
Missing legacy optional fields remain compatible.

Retained Node events must have strictly increasing positive `sequence` values. When `next_event_seq` is present it must be strictly greater than the retained tail, so a damaged counter cannot reuse an existing sequence. Legacy Nodes that predate `next_event_seq` remain readable; the next ingest, egress, or relay-client append derives its sequence from the retained tail plus one rather than from the retained list length. This remains correct after the bounded event history has dropped older entries.

Bootstrap-token records retain the existing consumed-only invariant. Their timestamps are finite non-negative numbers, including protection against integer-to-float overflow, and canonical attempt identity continues to be cross-checked against the referenced Node.

## Compatibility

`provider_server_id`, `boot_id`, `agent_version`, `status`, `desired_state`, `absolute_deadline`, and `created_at` existed in the first persisted Node record and are required. The access-token digest was already required by the earlier bootstrap hardening and remains required here.

Fields introduced later, including `session_assigned`, Destination/egress metadata, observations, event history, and related counters, remain optional for older records. When present they must satisfy the current safety-relevant type and enum contracts; validation does not synthesize missing legacy fields or rewrite the file.

## Reaper behavior

The Reaper now uses the same validated, non-mutating Node snapshot reader. A missing or untrusted registry skips heartbeat enforcement for that sweep rather than treating every Node as absent and tearing down otherwise healthy Sessions. Other independent timeout and cleanup work continues.

## Recovery

Do not delete initialization markers, replace `nodes.json` with an empty object, reset `next_node_seq`, or remove consumed token records to restore service. Restore a known-good authority snapshot or reconcile it against Session/provider evidence first. The validator deliberately fails closed instead of guessing a repaired state.

`max_concurrent_sessions` remains a separate product/policy decision under issue #87 and is not changed by this hardening.
