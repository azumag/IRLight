# Validate a proposed Media Node `max_sessions`

Issue #13 requires Media Node capacity to come from measured load rather than a guessed concurrency target. After a complete schema-v1 Node-capacity coverage manifest has been rendered and validated, `scripts/validate-node-capacity-max-sessions.py` provides a final read-only guardrail for a proposed scheduler / Node-inventory `max_sessions` value.

```bash
python3 scripts/validate-node-capacity-max-sessions.py \
  docs/evidence/node-capacity/coverage.json \
  --max-sessions 6 \
  --node-profile 'cx42-equivalent / 4 vCPU / 8 GiB / qa-image-v1' \
  --software-revision 0123456789abcdef0123456789abcdef01234567 \
  --json
```

The command first runs the canonical complete-coverage validator. It therefore rechecks the load plan, every required scenario, every planned concurrency level, report profile/scenario binding, common Node profile, exact software revision, common safety margin, and distinct measured run IDs. It then requires the validated Node profile and software revision to match the proposed deployment exactly before comparing the proposed positive integer with the conservative `recommended_max_sessions` derived from the complete scenario set. This prevents a capacity result measured on a different Node shape or software build from being silently reused for a new deployment.

A proposal is accepted only when it is **at or below** the measured recommendation. A lower value is allowed because an operator may intentionally reserve additional headroom for rollout or operational reasons; this validator does not invent or require that extra policy. A value above the measured recommendation fails closed and needs new measured evidence rather than an override flag.

The command does not edit scheduler inventory, Node configuration, deployment files, or production state. It does not execute a load test, contact a provider, use credentials, choose acceptance thresholds, choose the 720p/1080p workload mix, or choose the safety margin. Those inputs remain explicit measurement and operational decisions. The command only answers whether a supplied deployment identity and capacity value are bounded by the already-validated evidence.

Successful plain-text output contains numeric capacity values only. `--json` additionally includes the validated Node profile, software revision, report count, and safety margin so an operator can preserve provenance in a change review. Evidence-controlled validation errors are collapsed to fixed messages at this layer rather than reflected into terminal/CI output.

## Render a review artifact

After validation, `scripts/render-node-capacity-max-sessions-proposal.py` can emit a deterministic schema-v1 proposal for code review or a change record:

```bash
python3 scripts/render-node-capacity-max-sessions-proposal.py \
  docs/evidence/node-capacity/coverage.json \
  --max-sessions 6 \
  --node-profile 'cx42-equivalent / 4 vCPU / 8 GiB / qa-image-v1' \
  --software-revision 0123456789abcdef0123456789abcdef01234567 \
  > docs/evidence/node-capacity/max-sessions-proposal.json
```

The renderer runs the same canonical guardrail before emitting anything and additionally requires the coverage manifest itself to resolve to a canonical, symlink-free path inside the repository. The artifact records the repository-relative coverage-manifest path, deployment identity, candidate and measured recommendation, remaining headroom, report count, and safety margin. It does not apply the candidate to any scheduler or Node.

## Revalidate a persisted proposal

Before using a persisted proposal in a deployment review, `scripts/validate-node-capacity-max-sessions-proposal.py` can reconstruct the canonical proposal from its referenced measured evidence and require an exact match:

```bash
python3 scripts/validate-node-capacity-max-sessions-proposal.py \
  docs/evidence/node-capacity/max-sessions-proposal.json \
  --node-profile 'cx42-equivalent / 4 vCPU / 8 GiB / qa-image-v1' \
  --software-revision 0123456789abcdef0123456789abcdef01234567
```

The persisted proposal must be a regular, symlink-free repository file with bounded valid UTF-8 JSON, no duplicate keys, the exact schema-v1 field set, and strict JSON types. The validator reruns the canonical renderer against the referenced coverage manifest and the explicitly supplied deployment identity, then compares canonical JSON values. This catches edited candidate or derived values, stale recommendation/headroom/report-count/safety-margin fields, Boolean-for-integer substitutions, unexpected fields, and reuse against a different Node profile or software revision. Successful plain-text output contains numeric capacity values only; evidence- and operator-controlled strings are not reflected into validation errors.

The proposal's `coverage_manifest` path is provenance, not a digest by itself. For a durable review record, persist a Node-capacity review bundle with `scripts/write-node-capacity-review-bundle.py`. The bundle pins the proposal, coverage manifest, canonical load plan, and every measured scenario report by content digest, so later review does not silently follow different bytes at the same repository paths.

```bash
python3 scripts/write-node-capacity-review-bundle.py \
  docs/evidence/node-capacity/max-sessions-proposal.json \
  --node-profile 'cx42-equivalent / 4 vCPU / 8 GiB / qa-image-v1' \
  --software-revision 0123456789abcdef0123456789abcdef01234567 \
  --output docs/evidence/node-capacity/review-bundle.json

python3 scripts/validate-node-capacity-review-bundle.py \
  docs/evidence/node-capacity/review-bundle.json \
  --node-profile 'cx42-equivalent / 4 vCPU / 8 GiB / qa-image-v1' \
  --software-revision 0123456789abcdef0123456789abcdef01234567
```

Use the atomic writer rather than shell redirection for a persisted review bundle. It renders and validates the complete evidence closure before replacing the requested artifact, so a failed regeneration does not truncate the previous known-good bundle. The standalone renderer remains useful for transient pipeline output.

These guardrails are intentionally separate from applying a production capacity change. Wiring a measured value into the real scheduler / Node inventory must preserve the repository's deployment and review controls and should reference the validated review bundle used for the decision.
