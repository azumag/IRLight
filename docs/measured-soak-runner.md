# Measured soak evidence runner

`scripts/run-measured-soak.py` is the top-level entry point for producing a
reviewable soak-test evidence bundle from the disposable PoC Compose stack. It
does not replace the lower-level tools. Instead, it chains their existing
safety contracts:

1. `scripts/soak-compose.sh` creates and owns a unique `irlight-poc-soak-*`
   Compose project, performs liveness checks, starts periodic sample
   collection, tears the project down, and verifies project-scoped cleanup.
2. `scripts/assemble-soak-report.py` turns the raw JSONL samples into the
   versioned schema-v1 report.
3. `scripts/validate-soak-report.py` independently validates that report and
   emits a deterministic summary.
4. The wrapper writes `run-result.json` so an interrupted or failed run still
   has a small machine-readable receipt describing what evidence exists.

The wrapper exits 0 only when the soak runner exits 0, cleanup therefore has
been verified, the final report is assembled, and the canonical validator
accepts it. It refuses to reuse an existing output directory, so a subsequent
run cannot silently overwrite earlier evidence.

## Default 10-minute diagnostic run

Repository guidance treats about ten minutes as the normal Docker runtime
check. A media probe snapshot is required for measured media evidence:

```bash
python3 scripts/run-measured-soak.py \
  --output-dir evidence/soak-20260917-0500 \
  --scenario "10-minute integrated PoC soak" \
  --media-metrics-file /absolute/path/to/media-metrics.json
```

The media metrics file is the same strict snapshot consumed by
`collect-soak-sample.py` / `orchestrate-soak-samples.py`; the producer is
responsible for keeping it current throughout the run.

For resource-only diagnostics where media metrics are deliberately unavailable,
use the explicit diagnostic mode:

```bash
python3 scripts/run-measured-soak.py \
  --output-dir evidence/soak-resource-only-20260917-0500 \
  --scenario "resource-only diagnostic" \
  --allow-unmeasured-media
```

`--allow-unmeasured-media` is not release-quality evidence for bitrate,
A/V-sync, timestamp errors, or reconnect behavior. Do not use a diagnostic-only
run to claim the media acceptance criteria passed.

## Linux CI evidence-chain smoke

Pull-request CI runs `scripts/smoke-measured-soak-evidence.sh` on the existing
shared Linux Docker runner after the regular Docker integration suite. The smoke
uses an eight-second, two-second-interval resource-only run so it exercises the
real Compose lifecycle, sample collector, cleanup verifier, report assembler,
and canonical validator without adding a second cold Docker runner.

The smoke also revalidates the persisted `report.json` independently and checks
that the caller-visible evidence bundle contains the required baseline and a
final sample covering the declared duration. This catches wiring or filesystem
regressions that unit tests of the individual Python helpers cannot prove.

This CI smoke is deliberately `diagnostic-unmeasured`: it proves the end-to-end
evidence machinery executes on a real Linux Docker host, but it does **not**
prove media quality, leak thresholds, six-hour stability, or release
acceptance. Those remain separate measured runs with a live media probe.

## Release-candidate duration

Issue #13 requires a measured run of at least six hours for release acceptance.
Long runs are intentionally not the default. When a release candidate actually
needs that evidence, run it on an appropriate Linux Docker host with a live
media metrics probe:

```bash
python3 scripts/run-measured-soak.py \
  --output-dir evidence/rc-soak-20260917 \
  --scenario "release-candidate 6-hour measured soak" \
  --duration-seconds 21600 \
  --interval-seconds 30 \
  --media-metrics-file /absolute/path/to/media-metrics.json
```

A successful six-hour run is evidence collection, not permission to invent
resource thresholds. Memory growth, FD growth, CPU, A/V drift, and bitrate
acceptance limits still need an explicitly approved policy based on PoC data.

## Evidence bundle

A run uses an exclusive output directory containing:

- `samples.jsonl`: append-only periodic raw observations. The collector fsyncs
  each observation so partial evidence normally survives an interrupted run.
- `report.json`: schema-v1 assembled report, created only when raw samples can
  be validated for the declared outcome.
- `summary.json`: deterministic output of the canonical report validator.
- `run-result.json`: wrapper receipt containing the run UUID, scenario, target
  duration, sample interval, media mode, soak exit status, cleanup-verification
  status, evidence filenames, and any evidence-layer error.

For a successful run, `cleanup_verified` in both the report and receipt is true
only because `soak-compose.sh` returned 0 after its own project-scoped cleanup
verifier succeeded.

For a non-zero soak exit, the wrapper conservatively records cleanup as
unverified. `soak-compose.sh` preserves the original soak error when both the
soak and cleanup have problems, so a non-zero exit cannot prove cleanup by
itself. If partial samples exist, the wrapper still attempts to assemble and
validate a `fail` report without claiming cleanup success.

## Safety boundaries

- The wrapper cannot select a production/shared Compose project. Project naming
  remains owned by `soak-compose.sh`.
- It performs no provider API calls and creates no billable cloud resources.
- Existing evidence directories are never reused or overwritten.
- Measured media evidence and explicit unmeasured diagnostic mode are mutually
  exclusive.
- The canonical validator accepts only a stable regular `report.json` up to
  2 MiB. It rejects symlinks/special files and fails closed if the opened file
  or final pathname is replaced or mutated while the bounded read is in
  progress.
- A soak that appears successful but fails to produce a validated report and
  summary is converted to wrapper failure instead of being reported green.
