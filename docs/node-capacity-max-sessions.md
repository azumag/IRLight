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

The proposal path is a provenance reference, not a cryptographic content pin. Preserve the rendered proposal and referenced coverage manifest in the same reviewed Git revision (or otherwise preserve the exact immutable revision containing both) so a later reviewer cannot accidentally associate the proposal with a different version of the evidence at the same path.

This guardrail and renderer are intentionally separate from applying a production capacity change. Wiring a measured value into the real scheduler / Node inventory must preserve the repository's deployment and review controls, and should reference the exact coverage manifest and proposal revision used for the decision.
