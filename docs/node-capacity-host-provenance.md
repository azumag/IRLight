# Node capacity host-preflight provenance closure

Issue #599 added an opt-in provenance layer that binds persisted Node-capacity measurements to the validated host snapshot captured before load begins. Existing load-plan and coverage schemas remain unchanged, review-bundle schemas v1/v2 remain readable, and an explicit host-provenance binding can now be carried by additive review-bundle schema v3.

## Why a separate sidecar

Coverage schema v2 already proves that every persisted report can be reconstructed from one canonical load plan, its raw trials, and its runner manifest. The preflight-gated runner can also persist a validated `*.host-preflight.json`, but that file was not part of the durable evidence chain. A reviewer could therefore accidentally pair a valid measured run with the host snapshot from another run.

The host-provenance sidecar closes that gap without silently changing the meaning of existing schemas. Schema v1 pins:

- the exact coverage-v2 manifest bytes,
- the exact load-plan bytes,
- each scenario's exact report, raw-trials, and run-manifest bytes,
- each scenario's exact validated host-preflight bytes.

Every pin contains a canonical repository-relative path and a lowercase SHA-256 digest. A shared host-preflight file may be bound to several scenarios when they were measured on the same host snapshot; separate per-scenario preflights are also supported and represented explicitly.

```json
{
  "schema_version": 1,
  "coverage": {
    "path": "evidence/node-capacity/coverage.json",
    "sha256": "..."
  },
  "load_plan": {
    "path": "evidence/node-capacity/plan.json",
    "sha256": "..."
  },
  "scenarios": [
    {
      "scenario_id": "normal-input",
      "host_preflight": {
        "path": "evidence/node-capacity/host-preflight.json",
        "sha256": "..."
      },
      "report": {
        "path": "evidence/node-capacity/normal-input.report.json",
        "sha256": "..."
      },
      "trials": {
        "path": "evidence/node-capacity/normal-input.trials.jsonl",
        "sha256": "..."
      },
      "run_manifest": {
        "path": "evidence/node-capacity/normal-input.run.json",
        "sha256": "..."
      }
    }
  ]
}
```

## Rendering

Start from a coverage manifest that already uses schema v2 and passes `validate-node-capacity-coverage-manifest.py`. Supply exactly one host-preflight binding for every scenario in that coverage manifest. For the canonical Issue #13 matrix, for example:

```bash
python scripts/render-node-capacity-host-provenance.py \
  evidence/node-capacity/coverage.json \
  --preflight normal-input=evidence/node-capacity/host-preflight.json \
  --preflight all-holding=evidence/node-capacity/host-preflight.json \
  --preflight reconnect-storm=evidence/node-capacity/host-preflight.json \
  --preflight asset-prefetch=evidence/node-capacity/host-preflight.json \
  --preflight api-dashboard=evidence/node-capacity/host-preflight.json \
  > evidence/node-capacity/host-provenance.json
```

The exact scenario set is determined by the canonical coverage result; missing or extra bindings fail closed. Before the host-preflight bytes are hashed, the renderer applies the strict validator added by #598. Unknown fields, duplicate JSON keys, non-finite numbers, non-Linux snapshots, unsafe scalar values, symlinks, replacements during read, and oversized preflight files are therefore rejected rather than pinned.

The renderer is read-only. It does not execute a load test, contact an external provider, access credentials, mutate scheduler inventory, or change `max_sessions`.

## Validation

Validate a persisted sidecar with:

```bash
python scripts/validate-node-capacity-host-provenance.py \
  evidence/node-capacity/host-provenance.json --json
```

Validation requires the referenced coverage manifest to remain schema v2 and provenance-bound. Paths must be canonical repository-relative regular files and must not traverse symlinks. The validator checks every pinned digest, re-runs the existing coverage-v2 semantic validation, re-validates every host-preflight snapshot, and then re-reads all pinned files after semantic validation so a replacement during validation fails closed.

Changing only whitespace in a report, trials file, run manifest, load plan, coverage manifest, or host-preflight file changes its digest and invalidates the closure. This intentionally distinguishes byte-identical evidence from merely semantically equivalent evidence.

## Review-bundle integration

PR #601 completed the review-bundle integration that originally followed this sidecar work. Supplying a validated host-provenance sidecar explicitly with `--host-provenance` upgrades a provenance-bound review bundle to schema v3. Schema v3 pins the sidecar itself and embeds the canonical scenario-to-host-preflight path/SHA-256 mapping, then re-runs the nested host-provenance and host-preflight validation when the persisted review bundle is checked.

For example:

```bash
python3 scripts/write-node-capacity-review-bundle.py \
  docs/evidence/node-capacity/max-sessions-proposal.json \
  --host-provenance docs/evidence/node-capacity/host-provenance.json \
  --output docs/evidence/node-capacity/review-bundle.json \
  --node-profile 'linux-x86_64 4 vCPU 8 GiB' \
  --software-revision 0123456789abcdef0123456789abcdef01234567
```

Existing review-bundle schema v1/v2 remains valid when host provenance is not explicitly supplied. The schema-v3 atomic writer also refuses to overwrite the pinned host-provenance sidecar or any pinned host-preflight evidence.

## Compatibility and remaining boundary

This provenance work is additive. Coverage v1/v2 and review-bundle v1/v2 continue to validate exactly as before; nothing automatically upgrades old evidence or changes release policy. Review-bundle v3 is opt-in and is the current mechanism for including host provenance in the durable review closure.

Issue #599 is complete. Remaining Issue #13 work is operational rather than a missing provenance-linkage step: collect the required real measured evidence on approved hardware/environments, review thresholds and safety margins, and only then decide whether a production `max_sessions` change is justified. Production Node selection, acceptance thresholds, safety margin selection, provider resources, external streaming targets, credentials, and applying a production `max_sessions` value remain outside this documentation change.
