# Manual compatibility evidence reports

Manual device and external-platform checks are compatibility evidence only when a sanitized JSON report is committed in this directory and the corresponding `docs/compatibility-matrix.json` entry references it with `status=manual_verified`.

Run the repository-level checks with:

```sh
python3 scripts/validate-compatibility-matrix.py
python3 scripts/validate-manual-compatibility-reports.py
```

The normal unit test suite also exercises the manual-report contract.

## Report contract

A report uses `schema_version: 1` and records the matrix `entry_id` and `coverage` it supports. `tested_at` must be an ISO 8601 timestamp with an explicit timezone. The subject/application version, execution environment or device/OS, transport, media profile, network conditions, overall result, and concrete checks must be recorded.

Example shape:

```json
{
  "schema_version": 1,
  "entry_id": "obs-real-device",
  "coverage": "pc.obs",
  "tested_at": "2026-09-16T05:30:00+09:00",
  "subject": "OBS Studio",
  "version": "32.2.1",
  "environment": "macOS test host; device/OS details recorded here",
  "transport": "RTMPS",
  "profile": "1080p30 H.264/AAC",
  "network": "wired LAN to test IRLight node",
  "result": "PASS",
  "checks": [
    {"name": "publish accepted", "result": "PASS"},
    {"name": "disconnect and recover", "result": "PASS"}
  ],
  "notes": "Sanitized result; no credentials recorded."
}
```

Allowed overall results are `PASS`, `PARTIAL`, `FAIL`, and `BLOCKED`. A report referenced by a `manual_verified` matrix entry must be `PASS`; partial, failed, or blocked runs can be retained as investigation evidence but must not be promoted into a compatibility support claim.

Check results are `PASS`, `FAIL`, `BLOCKED`, or `NOT_APPLICABLE`. For `manual_verified` evidence, every applicable check must be `PASS`, no check may be `FAIL` or `BLOCKED`, and at least one check must actually be `PASS`; an all-`NOT_APPLICABLE` report cannot establish compatibility.

## Secret boundary

Never commit real stream keys, passwords, passphrases, bearer/API tokens, cookies, private keys, credential-bearing URLs, raw authentication headers, or unredacted logs. The validator rejects common secret-bearing field names recursively, including separator/case variants such as `clientSecret` and `access-token`. It also rejects URL userinfo and URL query fields whose decoded names are recognized as secret-bearing (for example `passphrase`, `stream_key`, `token`, or `client_secret`) without echoing the URL value into the validation error. SRT `streamid` query values are rejected as evidence URLs as well because they can carry authentication and routing material; record only a sanitized description of the SRT mode instead of the live endpoint.

These checks are intentionally fail-closed guards, not a complete secret scanner. They do not make arbitrary report text safe to publish and do not replace manual sanitization before commit. In particular, redact logs and free-form diagnostics before copying them into `notes`, `checks`, or additional report fields.

Use opaque Session IDs and fixed dummy values only when an identifier is necessary to explain the test. Keep platform credentials and private keys outside the repository and CI.

## Updating the matrix

When a real-device/platform run passes:

1. Add a sanitized JSON report under this directory.
2. Update the matching matrix entry from `not_tested` to `manual_verified` (or add a distinct entry for the same declared coverage).
3. Reference the report path in `evidence`.
4. Run both validators and the unit suite.

The report's `entry_id` and `coverage` must exactly match the matrix entry, preventing an unrelated successful run from being reused as evidence for a different compatibility claim.
