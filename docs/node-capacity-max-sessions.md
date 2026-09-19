# Validate a proposed Media Node `max_sessions`

Issue #13 requires Media Node capacity to come from measured load rather than a guessed concurrency target. After a complete schema-v1 Node-capacity coverage manifest has been rendered and validated, `scripts/validate-node-capacity-max-sessions.py` provides a final read-only guardrail for a proposed scheduler / Node-inventory `max_sessions` value.

```bash
python3 scripts/validate-node-capacity-max-sessions.py \
  docs/evidence/node-capacity/coverage.json \
  --max-sessions 6 \
  --json
```

The command first runs the canonical complete-coverage validator. It therefore rechecks the load plan, every required scenario, every planned concurrency level, report profile/scenario binding, common Node profile, exact software revision, common safety margin, and distinct measured run IDs. It then compares the proposed positive integer with the conservative `recommended_max_sessions` derived from the complete scenario set.

A proposal is accepted only when it is **at or below** the measured recommendation. A lower value is allowed because an operator may intentionally reserve additional headroom for rollout or operational reasons; this validator does not invent or require that extra policy. A value above the measured recommendation fails closed and needs new measured evidence rather than an override flag.

The command does not edit scheduler inventory, Node configuration, deployment files, or production state. It does not execute a load test, contact a provider, use credentials, choose acceptance thresholds, choose the 720p/1080p workload mix, or choose the safety margin. Those inputs remain explicit measurement and operational decisions. The command only answers whether a supplied capacity value is bounded by the already-validated evidence.

Successful plain-text output contains numeric capacity values only. `--json` additionally includes the validated Node profile, software revision, report count, and safety margin so an operator can preserve provenance in a change review. Evidence-controlled validation errors are collapsed to a fixed message at this layer rather than reflected into terminal/CI output.

This guardrail is intentionally separate from applying a production capacity change. Wiring a measured value into the real scheduler / Node inventory must preserve the repository's deployment and review controls, and should reference the exact coverage manifest used for the decision.