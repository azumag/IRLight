# Node capacity provenance-bound coverage

Issue #475 extends the durable Node-capacity evidence chain beyond the measured report itself. Existing coverage schema v1 remains valid for compatibility, but it only binds a canonical load plan to one persisted report per scenario. It does not prove that those persisted reports were produced from the raw trials and runner sidecars introduced by the planned scenario harness.

Schema v2 adds that missing binding without changing schema-v1 semantics.

```json
{
  "schema_version": 2,
  "load_plan": "evidence/node-capacity/plan.json",
  "reports": [
    {
      "scenario_id": "normal-input",
      "path": "evidence/node-capacity/normal-input.report.json",
      "trials_path": "evidence/node-capacity/normal-input.trials.jsonl",
      "run_manifest_path": "evidence/node-capacity/normal-input.run.json"
    }
  ]
}
```

Every scenario entry contains three repository-relative files: the persisted canonical report, the normalized raw trial stream, and the run manifest emitted by `run-node-capacity-scenario.py`. The shared `load_plan` remains a top-level binding because every scenario must use the same canonical plan.

`scripts/validate-node-capacity-coverage-manifest.py` accepts both schema versions. For schema v2 it first applies the same canonical path, symlink and regular-file checks used by schema v1, then invokes `validate-node-capacity-planned-report.py` for every scenario. That validator reconstructs the report from the load plan, raw trials and run manifest and requires exact canonical equality with the persisted report. Only after every scenario passes that provenance check does the normal complete-coverage validator calculate the common Node profile, software revision, safety margin and conservative `recommended_max_sessions`.

The JSON summary exposes `provenance_bound`: schema v1 returns `false`; schema v2 returns `true`. This makes migration explicit for downstream consumers instead of silently strengthening the meaning of an existing schema version.

To render schema v2, provide complete `--report`, `--trials`, and `--run-manifest` bindings for the exact same scenario set:

```bash
python scripts/render-node-capacity-coverage-manifest.py \
  evidence/node-capacity/plan.json \
  --report normal-input=evidence/node-capacity/normal-input.report.json \
  --trials normal-input=evidence/node-capacity/normal-input.trials.jsonl \
  --run-manifest normal-input=evidence/node-capacity/normal-input.run.json \
  ...
```

Omitting all provenance bindings preserves the schema-v1 renderer behavior. Supplying only part of the provenance set fails closed; each report scenario must have exactly one raw-trials binding and one run-manifest binding. Reusing a report, trials file, or run manifest across scenario bindings is also rejected.

This change is read-only. It does not execute a load test, contact a provider, consume credentials, choose thresholds or a safety margin, edit scheduler inventory, or apply `max_sessions`. Release-acceptance enforcement and digest-pinned review-bundle migration are downstream policy steps and should require `provenance_bound=true` before treating this evidence as the durable release chain.
