# Soak test evidence format

Issue #13 requires long-running tests to measure memory/process growth, CPU, file-descriptor use, A/V drift, bitrate drift, timestamp errors, unexpected reconnects, and cleanup. `scripts/soak-compose.sh` is a bounded runtime smoke/soak runner, but a successful exit alone is not enough evidence for the 2h/6h/12h acceptance tiers. This document defines a versioned report format that can be validated without contacting external platforms or changing production state.

## Scope and safety

A soak report records observations; it does **not** decide product thresholds. In particular, the validator does not label a memory/FD delta, CPU peak, bitrate range, or A/V drift as acceptable. Those limits should come from measured PoC results and an explicit acceptance-policy decision rather than being hidden in tooling. The only conditions required for `outcome: "pass"` are structurally trustworthy evidence, coverage of the declared target duration, at least two samples, and verified cleanup.

Normal PR work should continue to use the roughly ten-minute bounded soak described in `AGENTS.md`. The 2h/6h/12h tiers from #13 remain explicit long-running operations and should only be run when that acceptance evidence is actually needed. External Twitch/YouTube/Kick tests still require test credentials and the platform-specific operational safeguards from #13; this report format does not authorize them.

## Schema version 1

Each report is a single JSON object with exactly these fields:

- `schema_version`: integer `1`.
- `run_id`: UUID identifying one run.
- `scenario`: non-empty description (maximum 200 characters), for example `RTMP 1080p30 baseline`.
- `target_duration_seconds`: positive integer chosen by the run operator.
- `outcome`: `pass`, `fail`, or `aborted`.
- `samples`: ordered observations. `elapsed_seconds` must increase strictly.
- `cleanup`: `{ "verified": boolean, "details": string }`. `details` must be non-empty, and a passing run requires `verified: true`.
- `notes`: bounded free text; do not put stream keys, tokens, destination URLs containing credentials, or other secrets here.

Every sample contains exactly:

- `elapsed_seconds`: non-negative finite number since the start of measurement.
- `memory_rss_bytes`: aggregate RSS for the processes/containers under test.
- `cpu_percent`: aggregate CPU percentage using the same measurement method for the whole run.
- `open_fds`: aggregate open file-descriptor count.
- `processes`: relevant live process count.
- `zombies`: zombie process count.
- `bitrate_bps`: measured media bitrate, or `null` when unavailable.
- `av_sync_drift_ms`: measured audio/video drift, or `null` when unavailable.
- `timestamp_errors`: cumulative timestamp-error counter.
- `unexpected_reconnects`: cumulative unexpected-reconnect counter.

Counters must not decrease. For a `pass` report, the first sample is the run baseline: `elapsed_seconds`, `timestamp_errors`, and `unexpected_reconnects` must all start at zero. Numeric booleans, negative counters, `NaN`, `Infinity`, duplicate JSON keys, unknown fields, and missing fields fail closed. A `pass` report must contain at least two samples and its final `elapsed_seconds` must be at least `target_duration_seconds`; failed or aborted runs may intentionally be shorter so failure evidence can still be preserved.

## Validation and summary

Run:

```bash
python3 scripts/validate-soak-report.py path/to/report.json
python3 scripts/validate-soak-report.py --json path/to/report.json
```

The JSON form emits deterministic review data including start/end/peak RSS, RSS delta, peak CPU, FD/process deltas, peak zombies, bitrate min/max, peak absolute A/V drift, final timestamp-error and reconnect counters, and cleanup status. These values are evidence, not hidden acceptance thresholds.

For a release-candidate report, keep the raw report in a dedicated evidence location or attach it to the relevant QA issue/PR, record the exact commit/configuration and measurement commands in the surrounding test report, and reference that artifact from the release acceptance checklist only after the human/approved policy has judged the deltas acceptable.

## Example

```json
{
  "schema_version": 1,
  "run_id": "31f6f236-6a61-4f5e-b575-c1057d41c72e",
  "scenario": "RTMP 1080p30 baseline",
  "target_duration_seconds": 60,
  "outcome": "pass",
  "samples": [
    {
      "elapsed_seconds": 0,
      "memory_rss_bytes": 314572800,
      "cpu_percent": 18.2,
      "open_fds": 162,
      "processes": 14,
      "zombies": 0,
      "bitrate_bps": 5000000,
      "av_sync_drift_ms": 4.0,
      "timestamp_errors": 0,
      "unexpected_reconnects": 0
    },
    {
      "elapsed_seconds": 60,
      "memory_rss_bytes": 316669952,
      "cpu_percent": 19.1,
      "open_fds": 162,
      "processes": 14,
      "zombies": 0,
      "bitrate_bps": 4975000,
      "av_sync_drift_ms": 6.0,
      "timestamp_errors": 0,
      "unexpected_reconnects": 0
    }
  ],
  "cleanup": {
    "verified": true,
    "details": "disposable compose project removed; no test containers remain"
  },
  "notes": "Synthetic publisher and local mock destination only."
}
```

This format deliberately separates evidence integrity from acceptance policy. Follow-up work can add a collector that produces these samples automatically and a separately approved policy layer for the Node capacity and 6h release criteria without changing the evidence schema silently.
