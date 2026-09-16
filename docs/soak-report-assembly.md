# Soak report assembly

Issue #13 requires long-running QA evidence that can be reviewed independently of the runner that produced it. `scripts/collect-soak-resource-sample.py` emits one schema-compatible JSON sample at a time, while `scripts/validate-soak-report.py` validates the final report. `scripts/assemble-soak-report.py` connects those two stages without changing production state or inventing acceptance thresholds.

## Raw sample log

Append each collector result as one JSON object per line. Keep the baseline as the first line and preserve chronological order:

```bash
python3 scripts/collect-soak-resource-sample.py \
  --project irlight-poc-soak-1234-5678 \
  --elapsed-seconds 0 \
  --media-metrics-file /tmp/irlight-media-metrics.json \
  >> /tmp/irlight-soak-samples.jsonl
```

Repeat collection at the configured interval. The assembler treats this JSONL file as raw evidence: blank lines, duplicate JSON keys, non-object lines, invalid UTF-8, `NaN`, and `Infinity` fail closed. The final ordering, counters, schema fields, target-duration coverage, and passing-run baseline are checked by the canonical soak report validator.

## Assemble a report

After the run and cleanup verification, assemble the evidence:

```bash
python3 scripts/assemble-soak-report.py \
  --samples-jsonl /tmp/irlight-soak-samples.jsonl \
  --run-id "$(python3 -c 'import uuid; print(uuid.uuid4())')" \
  --scenario "RTMP 1080p30 baseline" \
  --target-duration-seconds 21600 \
  --outcome pass \
  --cleanup-verified \
  --cleanup-details "disposable compose project removed; no test containers remain" \
  --notes "local mock destination; commit/config recorded in QA issue" \
  --output /tmp/irlight-soak-report.json
```

`--cleanup-verified` is deliberately explicit. A report claiming `outcome: pass` is rejected unless cleanup was verified, the first sample is the zero baseline, at least two samples exist, and the final sample covers the target duration. Failed or aborted runs may preserve shorter evidence by using `--outcome fail` or `--outcome aborted` and an explanatory cleanup status.

The assembler imports `validate-soak-report.py` and validates the in-memory report before emitting it, so it cannot silently drift to a separate interpretation of schema version 1. The output is deterministic JSON with sorted keys. When `--output` is used, an existing file is not overwritten, and the raw JSONL file cannot be used as the output path. This preserves the original samples for later review.

Run the validator independently before attaching evidence to a QA issue or release checklist:

```bash
python3 scripts/validate-soak-report.py --json /tmp/irlight-soak-report.json
```

## Safety boundary

The assembler performs local file reads and an optional new-file write only. It does not start, stop, restart, remove, or inspect containers; contact Twitch, YouTube, Kick, or another provider; alter routes/firewalls; or use credentials. Cleanup verification remains an operator/runner responsibility and must describe what was actually checked. Resource and media acceptance thresholds remain a separate policy decision based on measured PoC results.
