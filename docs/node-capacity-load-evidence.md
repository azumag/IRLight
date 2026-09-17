# Node capacity load-test evidence

Issue #13 requires Media Node `max_sessions` to come from measured load rather than a guessed concurrency target. `scripts/validate-node-capacity-report.py` defines a fail-closed evidence contract for that decision.

The validator does **not** decide acceptable CPU, memory, network, bitrate, latency, or media-quality thresholds. Those remain scenario/policy inputs for the actual load test. It validates that the recorded result has a reproducible Node/software identity, strictly increasing load levels, a measured passing level followed by a measured failing level, and an explicit safety margin. It then derives the candidate `max_sessions` from the highest passing concurrency.

## Required evidence

A report is schema version 1 and contains:

- `run_id`: unique UUID for the load-test run.
- `node_profile`: enough non-secret hardware/runtime description to identify the tested Node class (for example vCPU, memory and relevant accelerator/profile information).
- `software_revision`: exact lowercase 40-character Git commit SHA tested.
- `scenario`: the workload/profile mix and the acceptance policy used to classify each load level.
- `safety_margin_percent`: an explicit integer from 1 through 90. The validator does not choose this policy value.
- `trials`: at least two strictly increasing concurrency levels. Every trial records duration, pass/fail, resource peaks, failed Session count, and unexpected reconnect count.
- `notes`: optional operator context as a string.

A valid capacity boundary requires at least one passing load and at least one failing load above it. Once a load level fails, a higher load level may not be marked passing in the same report; such evidence is too ambiguous to derive a production cap without another controlled run. A passing trial must have zero failed Sessions and zero unexpected reconnects.

The derived value is:

```text
recommended_max_sessions = floor(highest_passing_sessions * (100 - safety_margin_percent) / 100)
```

If that rounds below one Session, the evidence is insufficient to establish a positive production capacity and validation fails rather than silently removing the safety margin.

## Example

The numbers below are illustrative only; they are not IRLight production thresholds or a recommended margin.

```json
{
  "schema_version": 1,
  "run_id": "8f75d865-5e7c-4ffd-a5dc-e73fab5f39e1",
  "node_profile": "example: linux-x86_64 4 vCPU 8 GiB",
  "software_revision": "0123456789abcdef0123456789abcdef01234567",
  "scenario": "example steady pass-through workload under an approved acceptance policy",
  "safety_margin_percent": 25,
  "trials": [
    {
      "concurrent_sessions": 1,
      "duration_seconds": 300,
      "outcome": "pass",
      "cpu_peak_percent": 34.0,
      "memory_rss_peak_bytes": 500000000,
      "egress_peak_bps": 5000000,
      "failed_sessions": 0,
      "unexpected_reconnects": 0
    },
    {
      "concurrent_sessions": 4,
      "duration_seconds": 300,
      "outcome": "pass",
      "cpu_peak_percent": 176.0,
      "memory_rss_peak_bytes": 900000000,
      "egress_peak_bps": 20000000,
      "failed_sessions": 0,
      "unexpected_reconnects": 0
    },
    {
      "concurrent_sessions": 8,
      "duration_seconds": 300,
      "outcome": "fail",
      "cpu_peak_percent": 365.0,
      "memory_rss_peak_bytes": 1700000000,
      "egress_peak_bps": 40000000,
      "failed_sessions": 1,
      "unexpected_reconnects": 0
    }
  ],
  "notes": "example only"
}
```

Validate and emit a deterministic summary:

```bash
python3 scripts/validate-node-capacity-report.py capacity-report.json --json
```

For the illustrative report the highest passing level is 4, the first failing level is 8, and a 25% policy margin produces `recommended_max_sessions=3`.

## Release acceptance boundary

This contract alone does **not** satisfy the `node-capacity-load` item in `docs/release-acceptance-checklist.json`. That item remains pending until a real load harness has exercised the intended Node profile and workload, the resulting repository evidence passes this validator, and the chosen safety margin has been approved for operations. Do not update scheduler inventory, provision infrastructure, or make provider/billing changes merely because a synthetic or example report validates.
