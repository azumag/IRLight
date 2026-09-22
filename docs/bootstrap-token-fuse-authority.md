# Bootstrap token rollback fuse authority

`bootstrap_tokens.json` is a legacy rollback ledger that remains security-sensitive even though the canonical Node authority is `nodes.json`. A consumed entry is written before the canonical Node authority commit so that a crash or rollback cannot make an already-used bootstrap token reusable.

## Timestamp contract

Every persisted `consumed_at` value must be a finite, non-negative number. Unix epoch `0.0` remains valid. Negative values, `NaN`, positive or negative infinity, and values that cannot be represented as a finite float are invalid authority state.

The writer applies the same contract before reading or mutating the rollback fuse. If the effective wall clock is invalid, it raises `NodeStateError` before creating or changing `bootstrap_tokens.json` or its initialization marker. Existing fuse bytes therefore remain untouched on clock-validation failure.

After the writer adds the consumed record in memory, it validates the complete payload with the same `_validate_tokens()` contract used by readers before the atomic publish. Validation failure is fail-closed and does not publish a partial or self-inconsistent fuse.

## Durability and rollback semantics

The legacy fuse remains write-ahead: a valid consumed record is durably published before the canonical `nodes.json` update. This can burn a token if the later canonical write fails, but it must never permit token reuse. The initialization marker and atomic-write ordering are unchanged by the clock validation.

This contract intentionally does not add a future-skew limit, timestamp-ordering policy, token TTL, or replay-policy change. Those require separate compatibility or product decisions.

## Regression coverage

`tests/test_node_bootstrap_fuse_clock.py` verifies that:

- negative and non-finite wall clocks do not create a fuse or marker;
- an existing fuse keeps identical bytes and mtime when the clock is invalid;
- epoch `0.0` produces a record accepted by the canonical token validator; and
- completed payload validation happens before publication.

Related: #572, parent #87.
