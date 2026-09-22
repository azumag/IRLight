# Read-only soak sample runner

Issue #13 needs repeatable resource/media observations during the 2h/6h/12h acceptance tiers. `scripts/collect-soak-resource-sample.py` deliberately collects only one observation at a time; `scripts/run-soak-sampling.py` adds bounded scheduling and durable JSONL capture without taking ownership of the Docker lifecycle.

## Safety boundary

The runner is read-only with respect to the Compose project. It delegates every observation to the repository's existing collector and therefore only inspects a disposable project whose name matches `irlight-poc-soak-*`. It never executes `docker compose up`, `stop`, `restart`, `down`, `rm`, volume deletion, provider operations, or external-platform operations. The collector executable is not operator-overridable; the runner always invokes the repository copy so an arbitrary helper cannot manufacture evidence under this interface.

The output path is created with mode `0600` and must not already exist. The runner refuses to overwrite prior evidence. Every sample is revalidated against the schema-v1 sample fields, written as one compact JSON object, flushed, and `fsync`ed before the next interval. If collection later fails or the operator interrupts the run, previously completed lines remain available as partial failure evidence.

The runner does **not** decide whether memory/FD/CPU/media drift is acceptable, does not mark cleanup as verified, and does not produce a schema-v1 `outcome: pass` report. The existing `validate-soak-report.py` contract remains the authority for the final report structure. Product thresholds and long-running acceptance still require the explicit policy/evidence decision described in `docs/soak-test-evidence.md`.

## Measured run

Keep the disposable PoC project running and update the media-metrics snapshot through the existing probe path. Then collect a series, for example:

```bash
python3 scripts/run-soak-sampling.py \
  --project irlight-poc-soak-rc-1234 \
  --duration-seconds 600 \
  --interval-seconds 30 \
  --media-metrics-file /tmp/irlight-media-metrics.json \
  --output /tmp/irlight-soak-samples.jsonl
```

The baseline is recorded with `elapsed_seconds=0`. Subsequent samples use actual monotonic elapsed time rather than pretending a delayed observation happened at its nominal schedule. If a collector call or host scheduling delay crosses one or more nominal intervals, those missed intervals are skipped rather than replayed as a catch-up burst. The runner targets the requested duration; if an observation is delayed beyond it, that delayed observation becomes the final coverage sample and records its actual elapsed time. This keeps elapsed timestamps strictly increasing and suitable for the final report.

Each child collector invocation has a bounded execution time (180 seconds by default). It can be adjusted explicitly with `--collector-timeout-seconds` when a slower local Docker host requires it. This timeout is an instrumentation bound, not a product acceptance threshold. Collector stdout and stderr are also consumed incrementally with a 64 KiB bound per stream; crossing either bound terminates the child and fails the run closed instead of buffering arbitrary output in runner memory.

## Resource-only diagnostics

For diagnostics where no media probe is intentionally available, the same explicit opt-in as the one-shot collector is required:

```bash
python3 scripts/run-soak-sampling.py \
  --project irlight-poc-soak-debug-1234 \
  --duration-seconds 600 \
  --interval-seconds 30 \
  --allow-unmeasured-media \
  --output /tmp/irlight-resource-samples.jsonl
```

Do not use `--allow-unmeasured-media` as evidence that timestamp errors, reconnects, bitrate, or A/V drift were measured. It has the same limited meaning documented for the one-shot collector.

## Interruption and failure

- A child collector timeout, stdout/stderr bound violation, or invalid/multi-line/non-finite/schema-incomplete JSON fails the run closed.
- A backward or non-finite monotonic clock fails the run closed rather than manufacturing elapsed time.
- `Ctrl-C` returns exit code 130 and keeps already-fsynced sample lines.
- Other sampling failures return exit code 2.
- A successful scheduled series returns exit code 0 and reports only the sample count/output path; it does not claim release acceptance.

After the media run is complete, perform the separate cleanup verification required by `docs/soak-test-evidence.md`, assemble the schema-v1 report from the captured samples, run `scripts/validate-soak-report.py`, and preserve the exact commit/configuration and measurement commands with the QA evidence.
