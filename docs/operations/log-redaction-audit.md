# Structured log redaction audit

Issue #11 requires JSON structured logs to be correlatable without persisting stream keys, SRT passphrases, authentication tokens, passwords, or other credentials. `apps/control-api/log_redaction_inspect_cli.py` is a read-only JSONL audit guardrail for captured logs before they are retained or attached to an incident.

## Usage

Pipe a captured JSONL stream through the inspector. The command reads stdin only, so it does not resolve or print the source pathname.

```sh
python apps/control-api/log_redaction_inspect_cli.py < captured.jsonl
```

The output contains only `status`, the number of non-empty records, and normalized violation counts. It never includes log values or field paths. Exit status is `0` for `SAFE`, `2` for `REVIEW_REQUIRED`, and `3` for structurally invalid JSONL.

The baseline schema requires `timestamp`, `level`, `service`, and `event_type`. Correlation fields such as `request_id`, `session_id`, `node_id`, `version`, and `reason_code` remain event-dependent and can be added without changing this audit contract.

## Secret checks

Sensitive key names such as `authorization`, `token`, `stream_key`, `srt_passphrase`, `password`, `api_key`, `client_secret`, and `cookie` must be `null` or one of the explicit redaction placeholders (`[REDACTED]`, `<redacted>`, `***`). The same rule applies recursively to nested objects and arrays. Key matching normalizes case, common camelCase boundaries, and punctuation separators, so names such as `accessToken`, `streamKey`, `apiKey`, and `clientSecret` are covered by the same policy.

Sensitive query parameters are checked in absolute URLs and URL-like relative values such as `/callback?token=...` or `?apiKey=...`. Absolute URLs containing userinfo (`scheme://user@host` or `scheme://user:password@host`) always require review; producers should omit the userinfo rather than trying to retain a credential-shaped URL in logs. Duplicate JSON object keys and non-finite JSON numbers are rejected so a producer cannot hide a sensitive field behind parser-dependent behavior.

This is a detection guardrail, not a sanitizer. It does not rewrite logs and must not be used to make an unsafe record safe after the fact. It also cannot reliably infer secrets embedded in arbitrary free text or URL path segments. Producers still need allow-list logging and redaction at the point where a record is constructed; an audit result of `SAFE` is not proof that arbitrary message text contains no secret.

## Incident handling

If the result is `REVIEW_REQUIRED` or `INVALID`, do not upload the captured JSONL to an issue, chat, or ticket. Keep the source in the existing restricted incident location, identify the producer from operational context, fix the producer-side redaction/schema problem, and repeat the audit against a newly captured sample. Secret rotation decisions remain part of the secret-leak runbook; this command intentionally performs no credential, provider, billing, or runtime mutations.
