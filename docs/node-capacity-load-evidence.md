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

The evidence tooling also places finite parser/storage bounds on one controlled run: raw trial JSONL is limited to 1 MiB, the canonical trial list is limited to 4,096 records, and a final report file is limited to 2 MiB. These are defensive tooling limits against corrupted or non-cooperating inputs causing unbounded memory or I/O use; they are **not** workload acceptance thresholds, a recommended load-test size, or a production capacity policy. Start a new run/file rather than bypassing the limits if an unusually large experiment reaches them.

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

The validator only accepts a stable regular-file report. Symbolic links and non-regular inputs such as FIFOs are rejected, the final path is opened without following a symlink, and the device/inode observed before and after open must match. The validator checks the file size before and after open and performs a bounded binary read before UTF-8 decoding, so an oversized report fails before JSON parsing rather than being read without limit.

For the illustrative report the highest passing level is 4, the first failing level is 8, and a 25% policy margin produces `recommended_max_sessions=3`.

## Assemble raw trial JSONL

A load harness may append one completed trial object per line to a raw JSONL file and only assemble the final report after the run. `scripts/assemble-node-capacity-report.py` provides that boundary without choosing a workload threshold, margin, or production capacity. Each JSONL line is parsed with duplicate-key and non-finite-number rejection, and the assembled object is passed through the canonical report validator before it can be emitted.

All run metadata remains explicit. The assembler does not infer the tested revision, Node profile, scenario policy, or safety margin:

```bash
python3 scripts/assemble-node-capacity-report.py \
  --trials-jsonl capacity-trials.jsonl \
  --run-id 8f75d865-5e7c-4ffd-a5dc-e73fab5f39e1 \
  --node-profile 'linux-x86_64 4 vCPU 8 GiB' \
  --software-revision 0123456789abcdef0123456789abcdef01234567 \
  --scenario 'approved steady pass-through workload' \
  --safety-margin-percent 25 \
  --output capacity-report.json
```

When `--output` is supplied, the destination must not already exist and must not alias the raw JSONL file. The assembler first takes a shared advisory lock on the same `capacity-trials.jsonl.lock` sidecar used by the recorder, so cooperating recorder processes cannot append while the raw evidence snapshot is being read. The raw JSONL itself must be a stable regular file: symbolic links and non-regular files are rejected, the file is opened without following a final symlink, and the device/inode observed before and after open must match. The 1 MiB file cap is checked before and after open and the read itself is bounded; the parser also refuses more than 4,096 trial records. This keeps malformed evidence from turning assembly into unbounded memory/I/O work while preserving the same evidence semantics. The validated report is written to a mode-`0600` temporary file in the destination directory, flushed and `fsync`ed, and only then published to the final path with a no-overwrite atomic hard-link operation. If validation, writing, or publication fails, a partially written final report path is not exposed. Omitting `--output` writes the validated report to stdout.

The sidecar lock only coordinates tools that honor it. A manual or otherwise non-cooperating writer can still mutate the JSONL outside this contract and must not be used during report assembly. The assembler does not execute a load test, provision a Node, mutate scheduler inventory, or contact a provider.

## Record raw trials safely

`scripts/record-node-capacity-trial.py` is the append boundary for a load harness that has already measured and classified one trial. The caller must supply the observed metrics and an explicit `pass` or `fail`; the recorder does not choose acceptance thresholds, infer an outcome, or select a safety margin.

Example:

```bash
python3 scripts/record-node-capacity-trial.py \
  --trials-jsonl capacity-trials.jsonl \
  --concurrent-sessions 4 \
  --duration-seconds 300 \
  --outcome pass \
  --cpu-peak-percent 176 \
  --memory-rss-peak-bytes 900000000 \
  --egress-peak-bps 20000000 \
  --failed-sessions 0 \
  --unexpected-reconnects 0
```

Before append, the recorder validates the new trial with the same canonical trial schema used by the report validator. Under an advisory sidecar lock (`capacity-trials.jsonl.lock`), it opens or creates the raw JSONL once without following a final symlink, verifies the regular-file device/inode, and keeps that same file descriptor pinned while it reads, parses, validates, checks the final record boundary, appends, flushes, and `fsync`s. The public path must still name that pinned inode immediately before and after publication, and the file size must still match the validated snapshot before the append. This prevents a path replacement between validation and append from redirecting the new trial into a different file. A malformed existing file, unexpected replacement, invalid sequence, evidence file above 1 MiB, or an append that would cross the 1 MiB boundary fails closed without adding the candidate trial. The canonical 4,096-trial limit is applied before publication as well. New evidence and lock files are created with mode `0600`, and symbolic-link evidence targets are refused.

If an append attempt fails after writing any bytes, the recorder truncates the pinned inode back to the validated pre-append size and `fsync`s that rollback. If the target was created by that failed attempt and is still the same empty inode afterward, the recorder removes it so a transient write failure does not leave an invalid empty run file behind. A rollback failure is reported together with the original recording failure rather than silently treating the evidence as valid. This protects ordinary runtime write/`fsync` failures; it is not a power-loss transaction boundary, and abrupt process/host loss or non-cooperating writers remain outside the guarantee.

The sidecar lock serializes **cooperating recorder processes** and the final assembler snapshot; direct/manual writers do not participate in that lock. A non-cooperating process can still mutate the already-open inode outside the advisory-lock contract; such writers must not be used during a controlled run. Treat one JSONL file as one monotonic controlled run. After a failed level has been recorded, a later higher level may also be recorded as failed, but a passing retest or a lower load requires a new run/file so the final boundary is not cherry-picked.

This recorder only persists measurements supplied by the load harness. It does not create traffic, provision a Node, contact a provider, update scheduler inventory, or decide production `max_sessions`. After the run has a real passing/failing boundary, assemble the report with `assemble-node-capacity-report.py` and validate it before any operational capacity change.

## Release acceptance boundary

This contract alone does **not** satisfy the `node-capacity-load` item in `docs/release-acceptance-checklist.json`. That item remains pending until a real load harness has exercised the intended Node profile and workload, the resulting repository evidence passes this validator, and the chosen safety margin has been approved for operations. Do not update scheduler inventory, provision infrastructure, or make provider/billing changes merely because a synthetic or example report validates.
