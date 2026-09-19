# Node capacity load-plan manifest

Issue #13 requires Node capacity to be measured rather than guessed. Before the measured trial recorder and canonical report can be used, the load harness needs a reproducible concurrency/scenario plan. `scripts/render-node-capacity-load-plan.py` provides that planning boundary without executing traffic or making an operational capacity decision.

The renderer always includes the Issue #13 baseline concurrency ladder `1, 2, 4, 8` and these required load categories: normal input, all Sessions simultaneously in HOLDING, reconnect storm, concentrated asset pre-fetch, and concurrent API/dashboard activity. Extra concurrency levels are explicit operator inputs and extend, rather than replace, the baseline ladder.

The renderer deliberately does **not** invent the exact 720p/1080p mix, acceptance thresholds, or safety margin. Those remain operator/test-policy inputs because Issue #13 requires 720p/1080p mixed testing but does not define a ratio or final pass/fail thresholds. Use a concrete one-line `--profile-label` that records what the harness will actually generate, and render separate plans when comparing different media mixes.

```bash
python3 scripts/render-node-capacity-load-plan.py \
  --profile-label '720p30/1080p30 mix: explicit harness profile qa-mix-v1' \
  --session-count 16 \
  --session-count 32 \
  > capacity-load-plan.json
```

The command is read-only and only writes JSON to stdout. It does not start publishers, call Docker, open network connections, provision infrastructure, contact a provider, use credentials, update scheduler inventory, choose `max_sessions`, or classify a trial as pass/fail.

## Validate a saved plan before a harness consumes it

`scripts/validate-node-capacity-load-plan.py` validates that a saved JSON plan still matches the exact canonical renderer contract before another tool or load harness consumes it:

```bash
python3 scripts/validate-node-capacity-load-plan.py capacity-load-plan.json --json
```

The validator rejects missing baseline levels, reordered or duplicated levels, scenario changes, unexpected fields, duplicate JSON keys, non-finite JSON constants, invalid UTF-8, and non-object roots. The input is limited to 256 KiB and must be a stable regular file. A final symlink or special file is refused, the file is opened without following a final symlink, and the device/inode/size/mtime/ctime identity is compared across open, bounded read, and the final pathname so path replacement or an in-place mutation fails closed instead of silently validating a mixed snapshot.

Validation does not turn a plan into evidence and does not execute any load. A valid result only means the manifest is one that the current repository renderer could have produced. It does not approve the media profile, acceptance thresholds, safety margin, provider cost, or production capacity.

## Verify that measured reports cover the plan

After the harness has produced one canonical measured capacity report per planned scenario, use `scripts/validate-node-capacity-plan-coverage.py` to make sure the evidence actually covers the scenario matrix instead of validating an unrelated single report:

```bash
python3 scripts/validate-node-capacity-plan-coverage.py capacity-load-plan.json \
  --report normal-input=normal-input-report.json \
  --report all-holding=all-holding-report.json \
  --report reconnect-storm=reconnect-storm-report.json \
  --report asset-prefetch=asset-prefetch-report.json \
  --report api-dashboard=api-dashboard-report.json \
  --json
```

Every plan scenario must have exactly one explicit binding and each bound file must already satisfy the canonical capacity-report validator. To bind a report to both the media mix and the scenario without changing the existing report schema, its human-readable `scenario` field must begin with the exact machine-checkable prefix `profile=<profile_label>; scenario=<scenario_id>;`; acceptance-policy detail can follow that prefix. Every concurrency level listed by the plan must be present in the bound report; additional measured levels are allowed when the harness needs to continue upward to find a failing boundary.

For example, the `normal-input` report for a plan rendered with profile label `qa-mix-v1` can use `scenario: "profile=qa-mix-v1; scenario=normal-input; acceptance policy v1"`.

A coverage set is coherent only when all reports use the same Node profile, exact software revision, and safety margin, and every scenario has a distinct measured `run_id`. The summary reports the conservative `recommended_max_sessions` as the minimum recommendation across the complete scenario set. This is a comparison/validation result only: it does not update scheduler inventory, decide the acceptance thresholds or safety margin, provision infrastructure, or execute traffic.

The explicit `SCENARIO_ID=REPORT.json` binding is intentional. The existing capacity-report schema keeps `scenario` human-readable so it can describe the workload and acceptance policy; the coverage tool does not infer scenario identity from filenames or arbitrary free-form prose.

## Persist complete coverage as release evidence

The CLI bindings above are convenient for an operator, but they are not durable evidence by themselves. Once the real sanitized plan and all measured reports have been committed to the repository, persist the binding in a small schema-v1 coverage manifest:

```json
{
  "schema_version": 1,
  "load_plan": "docs/evidence/node-capacity/capacity-load-plan.json",
  "reports": [
    {
      "scenario_id": "normal-input",
      "path": "docs/evidence/node-capacity/normal-input-report.json"
    },
    {
      "scenario_id": "all-holding",
      "path": "docs/evidence/node-capacity/all-holding-report.json"
    },
    {
      "scenario_id": "reconnect-storm",
      "path": "docs/evidence/node-capacity/reconnect-storm-report.json"
    },
    {
      "scenario_id": "asset-prefetch",
      "path": "docs/evidence/node-capacity/asset-prefetch-report.json"
    },
    {
      "scenario_id": "api-dashboard",
      "path": "docs/evidence/node-capacity/api-dashboard-report.json"
    }
  ]
}
```

To avoid hand-editing that binding, `scripts/render-node-capacity-coverage-manifest.py` can render the durable object directly from repository-relative evidence paths. It first runs the canonical coverage-manifest validator, then emits the report entries in the load plan's canonical scenario order, so incomplete coverage, mismatched reports, unsafe paths, or duplicate bindings fail before any JSON is emitted:

```bash
python3 scripts/render-node-capacity-coverage-manifest.py \
  docs/evidence/node-capacity/capacity-load-plan.json \
  --report normal-input=docs/evidence/node-capacity/normal-input-report.json \
  --report all-holding=docs/evidence/node-capacity/all-holding-report.json \
  --report reconnect-storm=docs/evidence/node-capacity/reconnect-storm-report.json \
  --report asset-prefetch=docs/evidence/node-capacity/asset-prefetch-report.json \
  --report api-dashboard=docs/evidence/node-capacity/api-dashboard-report.json \
  > docs/evidence/node-capacity/coverage.json
```

The renderer is read-only apart from stdout and deliberately accepts only repository-relative references. It does not execute a load test, contact a provider, use credentials, choose a threshold or safety margin, or update scheduler inventory or production `max_sessions`.

All paths are canonical repository-relative paths. Validate the durable object with:

```bash
python3 scripts/validate-node-capacity-coverage-manifest.py \
  docs/evidence/node-capacity/coverage.json \
  --json
```

The manifest validator is read-only. It rejects duplicate keys, unknown fields, non-canonical or escaping paths, symlinked referenced paths, non-regular files, oversized/unstable manifest input, and incomplete or inconsistent scenario coverage. It then reuses the canonical plan-coverage validator, so every planned scenario and load level, exact profile/scenario prefix, common Node profile/revision/safety margin, and distinct measured `run_id` are rechecked from the referenced files rather than trusted from a previously printed CLI summary.

A single valid capacity report is therefore not complete Node-capacity release evidence. When `node-capacity-load` is marked `satisfied`, the release-acceptance checklist requires at least one valid durable coverage manifest. The plan and report files can also be listed as supplementary evidence, but the coverage manifest is the machine-checkable binding that closes the scenario matrix.

The load plan itself is still not measured evidence. A real harness must execute the planned load against the intended Node profile, record each measured trial with `record-node-capacity-trial.py`, assemble it with `assemble-node-capacity-report.py`, validate the final reports, and only then commit the sanitized coverage manifest that references those artifacts. None of these validators approve a media mix, acceptance threshold, safety margin, provider cost, or production `max_sessions`; those remain explicit operational decisions.