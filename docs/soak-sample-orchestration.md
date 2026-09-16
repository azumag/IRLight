# Periodic soak sample orchestration

Issue #13 needs long-running evidence that is collected throughout a run rather than reconstructed after the fact. `scripts/orchestrate-soak-samples.py` schedules the existing read-only resource/media collector and writes an append-only JSONL evidence prefix that can later be assembled with `scripts/assemble-soak-report.py`.

## Safety boundary

The orchestrator does not start, stop, restart, or remove containers and does not contact an external streaming provider. It invokes only the repository's fixed `collect-soak-resource-sample.py` helper. That collector accepts only disposable `irlight-poc-soak-*` Compose project names and performs read-only Docker/process inspection.

The output JSONL is created with exclusive-create semantics. Existing evidence is never overwritten. Every successful sample is flushed and `fsync`ed immediately, so a later collector failure or interruption leaves the already-collected prefix available for a `fail` or `aborted` report. The output path is also rejected if it aliases the Compose file or media-metrics snapshot.

Scheduling uses a monotonic clock. A zero-second baseline is always collected first, periodic observations follow at the configured interval, and the last observation covers the declared duration. If a collection itself is slow enough to miss an interval, the runner records the real later observation instead of fabricating catch-up samples with timestamps at which no measurement occurred.

## Usage

Run this against an already-running disposable PoC Compose project. For release-quality evidence, provide a media-probe snapshot that is updated by the probe during the run:

```bash
python3 scripts/orchestrate-soak-samples.py \
  --project irlight-poc-soak-1234-5678 \
  --duration-seconds 21600 \
  --interval-seconds 30 \
  --media-metrics-file /tmp/irlight-media-metrics.json \
  --samples-jsonl /tmp/irlight-soak-samples.jsonl
```

For resource-only diagnostics, `--allow-unmeasured-media` can be used instead of `--media-metrics-file`. As with the single-sample collector, that mode must not be treated as proof that timestamp errors or reconnects were measured to be zero.

If any collector invocation fails, returns malformed JSON, times out, or cannot durably persist the sample, orchestration stops with a non-zero status. `Ctrl-C` returns 130 and preserves the JSONL prefix.

## Finish the evidence chain

After the workload is stopped, verify cleanup with the project-scoped verifier:

```bash
python3 scripts/verify-soak-cleanup.py --project irlight-poc-soak-1234-5678
```

Then assemble and independently validate the report as described in `docs/soak-report-assembly.md`. A passing report still requires verified cleanup and canonical schema validation; this orchestrator does not weaken those gates or invent resource/quality acceptance thresholds.

The remaining integration step for the built-in `scripts/soak-compose.sh` runner is to wire this periodic collector into the runner's internally generated disposable project lifecycle. Until that is done, use the orchestrator with an explicitly named disposable `irlight-poc-soak-*` project whose lifecycle is controlled by the QA run.
