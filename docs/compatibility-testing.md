# Compatibility evidence workflow

Issue #13 requires a compatibility matrix that distinguishes reproducible evidence from devices and platforms that have not actually been exercised. The canonical machine-readable inventory is `docs/compatibility-matrix.json`.

## Status rules

- `automated`: a repository-owned CI workflow or smoke test exercises the subject/transport. Every claim must point at the repository paths that produce the evidence.
- `manual_verified`: a real device/application/platform was exercised and a sanitized test report was committed. The report must identify versions, profile, network conditions, result, and date without storing credentials.
- `not_tested`: a required coverage slot that does not yet have evidence. It is a gap, not a compatibility claim.

Never promote FFmpeg or local MediaMTX results into claims about OBS, mobile apps, hardware encoders, Twitch, YouTube, or Kick. Those subjects need their own evidence. Real stream keys, access tokens, account identifiers, private endpoint URLs, and other credentials must not be committed to the matrix or a report.

Validate the matrix with:

```sh
python3 scripts/validate-compatibility-matrix.py
```

The normal unit suite also runs the same contract checks.

## Manual test report template

Create a sanitized report under `docs/compatibility-reports/` when a manual slot is actually exercised. A report should contain at least:

```text
Test ID: <stable id matching/replacing a matrix entry>
Date (UTC): <YYYY-MM-DD>
Tester: <role or non-sensitive handle>
Subject: <app/device/platform name>
Version: <app/firmware/OS version>
Transport: <RTMP|RTMPS|SRT>
Video: <resolution/fps/codec/profile/bitrate/GOP>
Audio: <codec/sample rate/channels/bitrate>
Network: <Wi-Fi/4G/5G/tethering/wired + sanitized conditions>
Scenario: <connect/disconnect/recover/long-run/etc.>
Expected: <observable contract>
Observed: <observable result>
Result: PASS | WARN | FAIL
Evidence: <sanitized log/artifact reference; no credentials>
Known limitations: <anything not covered>
```

If a result is platform-specific or depends on an account feature, record that limitation rather than generalizing it to all accounts or regions.

## Updating the matrix

When evidence is added, replace or update the matching coverage entry rather than leaving both a `not_tested` placeholder and a verified claim for the same exact test identity. Keep `required_coverage` explicit so deleting an untested mobile/hardware/platform row cannot make the dashboard look more complete.

Automated evidence must be an executable repository-owned test surface under `.github/workflows/`, `scripts/`, or `tests/`, and every referenced path must exist. A README, design note, or other documentation file is not sufficient by itself to promote a coverage slot to `automated`. This catches renamed/deleted smoke workflows and prevents a documentation-only reference from becoming a stale compatibility claim.

Manual evidence remains restricted to sanitized reports under `docs/compatibility-reports/`. Supporting logs or screenshots may be described from that report, but credentials and private account details must not be committed.

## Scope not covered by this slice

This inventory does not decide final support policy, safe Node capacity, the 2/6/12/24-hour soak acceptance thresholds, or external-platform test cadence. It only establishes a fail-closed evidence ledger so later real-device and long-running results can be recorded without confusing “not tested” with “supported.”
