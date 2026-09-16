# Periodic soak sample orchestration

Issue #13 needs long-running evidence that is collected throughout a run rather than reconstructed after the fact. `scripts/orchestrate-soak-samples.py` schedules the existing read-only resource/media collector and writes an append-only JSONL evidence prefix that can later be assembled with `scripts/assemble-soak-report.py`.

## Safety boundary

The orchestrator does not start, stop, restart, or remove containers and does not contact an external streaming provider. It invokes only the repository's fixed `collect-soak-resource-sample.py` helper. That collector accepts only disposable `irlight-poc-soak-*` Compose project names and performs read-only Docker/process inspection.

The output JSONL is created with exclusive-create semantics. Existing evidence is never overwritten. Every successful sample is flushed and `fsync`ed immediately, so a later collector failure or interruption leaves the already-collected prefix available for a `fail` or `aborted` report. The output path is also rejected if it aliases the Compose file or media-metrics snapshot.

Scheduling uses a monotonic clock. A zero-second baseline is always collected first, periodic observations follow at the configured interval, and the last observation covers the declared duration. If a collection itself is slow enough to miss an interval, the runner records the real later observation instead of fabricating catch-up samples with timestamps at which no measurement occurred.

## Standalone usage

Run the orchestrator directly against an already-running disposable PoC Compose project when another harness owns that project's lifecycle. For release-quality evidence, provide a media-probe snapshot that is updated by the probe during the run:

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

## Integrated Compose soak runner

`scripts/soak-compose.sh` can now own both the disposable Compose lifecycle and periodic evidence collection. Evidence collection is opt-in so existing short smoke/soak runs do not create unexpected artifacts. Set `SOAK_SAMPLES_JSONL` to a path that does not already exist and provide a media-probe snapshot for release-quality evidence:

```bash
SOAK_SECONDS=21600 \
SOAK_INTERVAL_SECONDS=30 \
SOAK_SAMPLES_JSONL=/tmp/irlight-soak-samples.jsonl \
SOAK_MEDIA_METRICS_FILE=/tmp/irlight-media-metrics.json \
./scripts/soak-compose.sh
```

The runner passes its internally generated `irlight-poc-soak-*` project name, the same declared duration, and the same interval to the orchestrator. It starts evidence collection only after the control API, HLS output, and node registration are ready. The existing liveness checks continue throughout the run, and the runner does not print success until the evidence child also exits successfully.

On an early liveness failure or interruption, cleanup terminates and reaps the evidence child before running project-scoped `docker compose down` and the cleanup verifier. This prevents the collector from racing teardown while preserving the already-`fsync`ed JSONL prefix.

Resource-only diagnostic collection must be explicitly requested rather than being silently substituted for media evidence:

```bash
SOAK_SECONDS=600 \
SOAK_SAMPLES_JSONL=/tmp/irlight-resource-only.jsonl \
SOAK_ALLOW_UNMEASURED_MEDIA=1 \
./scripts/soak-compose.sh
```

`SOAK_MEDIA_METRICS_FILE` and `SOAK_ALLOW_UNMEASURED_MEDIA=1` are mutually exclusive. A run using `SOAK_ALLOW_UNMEASURED_MEDIA=1` is not release evidence for timestamp errors, reconnects, bitrate, or A/V drift.

## Finish the evidence chain

After a standalone workload is stopped, verify cleanup with the project-scoped verifier:

```bash
python3 scripts/verify-soak-cleanup.py --project irlight-poc-soak-1234-5678
```

The integrated `soak-compose.sh` runner performs that cleanup verification automatically. In either mode, assemble and independently validate the report as described in `docs/soak-report-assembly.md`. A passing report still requires verified cleanup and canonical schema validation; the orchestrator and integrated runner do not weaken those gates or invent resource/quality acceptance thresholds.
