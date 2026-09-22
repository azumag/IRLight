# Node bootstrap transaction clock contract

The Node bootstrap transaction treats its wall-clock sample as part of the authority mutation boundary.

For a new bootstrap attempt, `_bootstrap_locked()` samples the effective wall clock once before Node ID allocation, Session binding, rollback-fuse publication, or canonical Node-authority publication. The sample must be a finite, non-negative number. Unix epoch `0.0` remains valid; negative values, `NaN`, positive or negative infinity, and values that cannot be represented as a finite float fail closed.

The same validated sample is reused for the canonical Node `created_at`, the canonical bootstrap-token `consumed_at`, and the legacy rollback fuse `consumed_at`. A later wall-clock change during the same bootstrap attempt therefore cannot make the rollback fuse and canonical authority disagree about the transaction timestamp.

The configured absolute Node deadline is derived from the validated bootstrap timestamp. The completed deadline must also be finite and non-negative before Session binding is attempted. An assigned Session's existing `absolute_deadline_at`, when present, is likewise checked before `bind_node()` so an invalid persisted/caller value cannot mutate Session state and then fail only when Node authority is published.

The existing write-ahead ordering is intentionally preserved: the legacy consumed-token fuse is published before the canonical `nodes.json` commit. If the canonical write fails after the fuse is durable, the bootstrap token may be burned, but it must never become reusable. This change does not introduce rollback of an already-bound Session or a new compensation protocol; those semantics require their own concurrency/fencing design.

This contract does not add a future-skew ceiling, a new ordering policy between independent timestamps, or a change to bootstrap-token replay/TTL behavior. It also does not create or delete provider resources.
