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
python3 scripts/validate-manual-compatibility-reports.py
```

The normal unit suite also runs the same contract checks.

The matrix itself is strict JSON with a closed top-level and entry schema and is limited to 256 KiB before parsing. Duplicate object keys and non-standard JSON constants (`NaN`, `Infinity`, `-Infinity`) are rejected, and `schema_version` must be the integer `1` rather than a JSON boolean. This keeps the compatibility ledger parser-independent and prevents undeclared fields from becoming an accidental support-claim channel.

The matrix input is also a bounded regular-file trust boundary. The validator rejects a final symlink, FIFO, device, or other non-regular input before opening it; opens the file without following a final symlink where the platform supports that flag; reads at most 256 KiB plus one byte; and verifies device/inode/size/mtime/ctime plus the final pathname identity across the read. A matrix replaced or modified while validation is in progress therefore fails closed instead of being parsed from a different byte sequence than the one initially inspected.

## Manual execution worksheet

Before serializing evidence, the tester may use the existing human-facing worksheet below to capture the observation. `WARN` is a triage result, not a verified compatibility claim: a warning must remain unverified until the evidence can be represented by the JSON schema below with report-level `PASS` and passing applicable checks.

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

## Manual test report schema

Create a sanitized JSON report under `docs/compatibility-reports/` only when a manual slot is actually exercised. Schema version 1 is deliberately closed: reports may contain only the fields shown below, and each `checks` object may contain only `name`, `result`, and optional `notes`. Additions require an explicit validator/schema change first rather than silently creating a new free-form evidence channel.

```json
{
  "schema_version": 1,
  "entry_id": "obs-real-device",
  "coverage": "pc.obs",
  "tested_at": "2026-09-16T05:30:00+09:00",
  "subject": "OBS Studio",
  "version": "32.2.1",
  "environment": "macOS 15, Apple Silicon test host",
  "transport": "RTMPS",
  "profile": "1080p30 H.264/AAC",
  "network": "wired LAN to local IRLight node",
  "result": "PASS",
  "checks": [
    {
      "name": "publish accepted",
      "result": "PASS",
      "notes": "Sanitized observation only"
    },
    {
      "name": "disconnect and recover",
      "result": "PASS"
    }
  ],
  "notes": "Sanitized compatibility evidence; no credentials recorded."
}
```

Report-level `result` is one of `PASS`, `PARTIAL`, `FAIL`, or `BLOCKED`. Check results are one of `PASS`, `FAIL`, `BLOCKED`, or `NOT_APPLICABLE`. A matrix entry may use `manual_verified` only when the bound report has `result=PASS`, every applicable check passes, and at least one check is `PASS`.

`tested_at` must include a timezone offset. `entry_id` and `coverage` must match the matrix entry exactly. Record versions, profile, environment, and network conditions precisely enough to reproduce the test, but keep account identities, private endpoints, credentials, and raw logs out of the report.

Evidence paths are resolved before use and the resolved file must remain inside `docs/compatibility-reports/`. A path traversal or symlink that resolves elsewhere in the repository is rejected rather than treated as manual evidence.

Manual report files are strict JSON and are limited to 64 KiB before parsing. Duplicate object keys and non-standard JSON constants (`NaN`, `Infinity`, `-Infinity`) are rejected so different JSON parsers cannot interpret the same evidence differently and a report cannot become an unbounded CI input.

## Secret boundary

Never commit real stream keys, passwords, passphrases, bearer/API tokens, cookies, private keys, credential-bearing URLs, raw authentication headers, or unredacted logs. The validator rejects common secret-bearing field names recursively, including separator/case variants such as `clientSecret` and `access-token`. It also rejects URL userinfo and URL query fields whose decoded names are recognized as secret-bearing (for example `passphrase`, `stream_key`, `token`, or `client_secret`) without echoing the URL value into the validation error. SRT `streamid` query values are rejected as evidence URLs as well because they can carry authentication and routing material; record only a sanitized description of the SRT mode instead of the live endpoint.

These checks are intentionally fail-closed guards, not a complete secret scanner. They do not make arbitrary report text safe to publish and do not replace manual sanitization before commit. In particular, redact logs and free-form diagnostics before copying them into `notes` or check `notes`.

Use opaque Session IDs and fixed dummy values only when an identifier is necessary to explain the test. Keep platform credentials and private keys outside the repository and CI.

## Updating the matrix

When evidence is added, replace or update the matching coverage entry rather than leaving both a `not_tested` placeholder and a verified claim for the same exact test identity. Keep `required_coverage` explicit so deleting an untested mobile/hardware/platform row cannot make the dashboard look more complete.

Automated evidence must point to a repository-owned automation surface under `.github/workflows/`, `scripts/`, or `tests/`, and every referenced path must exist. Workflow evidence must use `.yml` / `.yaml`; script or test evidence must use `.sh` / `.py`. A README, design note, or other documentation file is not sufficient by itself to promote a coverage slot to `automated`, even when it lives inside one of those directories. This catches renamed/deleted smoke workflows and prevents a documentation-only reference from becoming a stale compatibility claim.

Manual evidence remains restricted to sanitized JSON reports under `docs/compatibility-reports/`. Supporting logs or screenshots may be described from that report, but credentials and private account details must not be committed.

## Scope not covered by this slice

This inventory does not decide final support policy, safe Node capacity, the 2/6/12/24-hour soak acceptance thresholds, or external-platform test cadence. It only establishes a fail-closed evidence ledger so later real-device and long-running results can be recorded without confusing “not tested” with “supported.”
