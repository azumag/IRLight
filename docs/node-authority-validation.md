# Node authority validation

`nodes.json` is authoritative for Node identity, bootstrap-token consumption, heartbeat liveness, and stop intent. Corrupt state must not be interpreted as an empty registry, a reusable bootstrap token, or a missing heartbeat.

## Load and write contract

The Control Plane rejects non-standard JSON numeric constants (`NaN`, `Infinity`, `-Infinity`) and writes Node/token authority with `allow_nan=False`. Validation runs on canonical reads and before replacement writes. A rejected in-memory update therefore does not replace the last readable authority file.

Each Node must keep a matching `node_id`, non-empty Session/provider/boot/agent identity, a valid access-token SHA-256 digest, known `status` and `desired_state`, finite lifecycle timestamps, and correctly typed safety booleans/counters. `next_node_seq` is a strict positive integer and must remain ahead of canonical `node-NNNN` IDs so corruption cannot overwrite an existing Node on the next bootstrap. Present ingest, egress, relay-client observations and Node events are structurally checked, while every nested numeric value must be finite.

Bootstrap-token records retain the existing consumed-only invariant. Their timestamps are finite non-negative numbers, including protection against integer-to-float overflow, and canonical attempt identity continues to be cross-checked against the referenced Node.

## Compatibility

`provider_server_id`, `boot_id`, `agent_version`, `status`, `desired_state`, `absolute_deadline`, and `created_at` existed in the first persisted Node record and are required. The access-token digest was already required by the earlier bootstrap hardening and remains required here.

Fields introduced later, including `session_assigned`, Destination/egress metadata, observations, event history, and related counters, remain optional for older records. When present they must satisfy the current safety-relevant type and enum contracts; validation does not synthesize missing legacy fields or rewrite the file.

## Reaper behavior

The Reaper now uses the same validated, non-mutating Node snapshot reader. A missing or untrusted registry skips heartbeat enforcement for that sweep rather than treating every Node as absent and tearing down otherwise healthy Sessions. Other independent timeout and cleanup work continues.

## Recovery

Do not delete initialization markers, replace `nodes.json` with an empty object, reset `next_node_seq`, or remove consumed token records to restore service. Restore a known-good authority snapshot or reconcile it against Session/provider evidence first. The validator deliberately fails closed instead of guessing a repaired state.

`max_concurrent_sessions` remains a separate product/policy decision under issue #87 and is not changed by this hardening.
