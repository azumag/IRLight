"""Read-only JSONL audit for structured-log schema and secret redaction.

The inspector deliberately reports only normalized reason-code counts. It never
includes a log value, a field path, or a source pathname in its result so that
running the audit does not become a second secret-leak channel.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any, BinaryIO, TextIO
from urllib.parse import parse_qsl, urlsplit

_REQUIRED_FIELDS = ("timestamp", "level", "service", "event_type")
_REDACTED_VALUES = {"[redacted]", "<redacted>", "***"}
_MAX_RECORD_BYTES = 256 * 1024
_MAX_NESTING_DEPTH = 64
_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SEPARATORS = re.compile(r"[^0-9A-Za-z]+")
_SENSITIVE_KEYS = {
    "secret",
    "secret_key",
    "token",
    "access_token",
    "refresh_token",
    "auth_token",
    "authorization",
    "password",
    "passphrase",
    "srt_passphrase",
    "stream_key",
    "streamkey",
    "api_key",
    "apikey",
    "access_key",
    "access_key_id",
    "private_key",
    "client_secret",
    "cookie",
    "set_cookie",
}
_SENSITIVE_KEY_SUFFIXES = (
    "_secret",
    "_secret_key",
    "_token",
    "_authorization",
    "_password",
    "_passphrase",
    "_stream_key",
    "_api_key",
    "_access_key",
    "_access_key_id",
    "_private_key",
    "_cookie",
)
_SENSITIVE_COMPACT_SUFFIXES = tuple(
    suffix.removeprefix("_").replace("_", "") for suffix in _SENSITIVE_KEY_SUFFIXES
)
_SENSITIVE_URL_PARAMETER_KEYS = {
    "x_amz_credential",
    "x_amz_signature",
    "x_amz_security_token",
    "x_goog_credential",
    "x_goog_signature",
}
_GCS_V2_CONTEXT_KEYS = {"google_access_id", "expires"}
_AZURE_SAS_CONTEXT_KEYS = {
    "se",
    "si",
    "sp",
    "sr",
    "ss",
    "srt",
    "tn",
}


class _DuplicateKeyError(ValueError):
    pass


class _NonFiniteNumberError(ValueError):
    pass


class _OversizedRecord:
    pass


class _InvalidUtf8Record:
    pass


_OVERSIZED_RECORD = _OversizedRecord()
_INVALID_UTF8_RECORD = _InvalidUtf8Record()


def _reject_nonfinite(value: str) -> None:
    raise _NonFiniteNumberError(value)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _normalize_key(key: str) -> str:
    expanded = _ACRONYM_BOUNDARY.sub("_", key.strip())
    expanded = _CAMEL_CASE_BOUNDARY.sub("_", expanded)
    return _KEY_SEPARATORS.sub("_", expanded).strip("_").lower()


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalize_key(key)
    if normalized in _SENSITIVE_KEYS or any(
        normalized.endswith(suffix) for suffix in _SENSITIVE_KEY_SUFFIXES
    ):
        return True
    compact = normalized.replace("_", "")
    return any(compact.endswith(suffix) for suffix in _SENSITIVE_COMPACT_SUFFIXES)


def _is_sensitive_url_parameter(key: str) -> bool:
    return _is_sensitive_key(key) or _normalize_key(key) in _SENSITIVE_URL_PARAMETER_KEYS


def _is_redacted(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and value.strip().lower() in _REDACTED_VALUES


def _url_for_secret_scan(value: str):
    candidate = value.strip()
    if "://" not in candidate and not candidate.startswith(("/", "./", "../", "?", "#")):
        return None
    try:
        return urlsplit(candidate)
    except ValueError:
        return None


def _component_has_unredacted_sensitive_params(component: str) -> bool:
    try:
        params = parse_qsl(component, keep_blank_values=True)
    except ValueError:
        return False
    normalized_keys = {_normalize_key(key) for key, _ in params}
    is_gcs_v2 = _GCS_V2_CONTEXT_KEYS.issubset(normalized_keys)
    is_azure_sas = "sv" in normalized_keys and bool(
        normalized_keys & _AZURE_SAS_CONTEXT_KEYS
    )
    for key, raw_value in params:
        normalized = _normalize_key(key)
        provider_context_sensitive = (
            normalized == "signature" and is_gcs_v2
        ) or (normalized == "sig" and is_azure_sas)
        if (
            _is_sensitive_url_parameter(key) or provider_context_sensitive
        ) and not _is_redacted(raw_value):
            return True
    return False


def _url_has_unredacted_sensitive_query(value: str) -> bool:
    parsed = _url_for_secret_scan(value)
    if parsed is None:
        return False
    return _component_has_unredacted_sensitive_params(parsed.query)


def _url_has_unredacted_sensitive_fragment(value: str) -> bool:
    parsed = _url_for_secret_scan(value)
    if parsed is None:
        return False
    return _component_has_unredacted_sensitive_params(parsed.fragment)


def _url_has_userinfo(value: str) -> bool:
    parsed = _url_for_secret_scan(value)
    if parsed is None or "://" not in value:
        return False
    try:
        return parsed.username is not None or parsed.password is not None
    except ValueError:
        return False


def _scan_value(value: Any, reasons: Counter[str]) -> bool:
    too_deep = False
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > _MAX_NESTING_DEPTH:
            too_deep = True
            continue
        if isinstance(current, Mapping):
            for key, child in current.items():
                if _is_sensitive_key(str(key)) and not _is_redacted(child):
                    reasons["SENSITIVE_FIELD_UNREDACTED"] += 1
                stack.append((child, depth + 1))
            continue
        if isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
            continue
        if isinstance(current, str):
            if _url_has_unredacted_sensitive_query(current):
                reasons["SENSITIVE_URL_QUERY_UNREDACTED"] += 1
            if _url_has_unredacted_sensitive_fragment(current):
                reasons["SENSITIVE_URL_FRAGMENT_UNREDACTED"] += 1
            if _url_has_userinfo(current):
                reasons["SENSITIVE_URL_USERINFO"] += 1
    if too_deep:
        reasons["NESTING_TOO_DEEP"] += 1
    return too_deep


def _inspect_record(record: Any, reasons: Counter[str]) -> bool:
    if not isinstance(record, dict):
        reasons["INVALID_RECORD"] += 1
        return True
    missing = [name for name in _REQUIRED_FIELDS if name not in record]
    if missing:
        reasons["MISSING_REQUIRED_FIELD"] += 1
    invalid_required = [
        name
        for name in _REQUIRED_FIELDS
        if name in record
        and (not isinstance(record[name], str) or not record[name].strip())
    ]
    if invalid_required:
        reasons["INVALID_REQUIRED_FIELD"] += 1
    return _scan_value(record, reasons)


def _iter_bounded_lines(stream: TextIO) -> Iterable[str | _OversizedRecord]:
    """Bound text streams used by tests/in-process callers.

    TextIO.readline() limits characters rather than encoded bytes, so the CLI
    prefers `_iter_bounded_byte_lines()` whenever a raw buffer is available.
    """
    read_limit = _MAX_RECORD_BYTES + 1
    while True:
        line = stream.readline(read_limit)
        if line == "":
            return
        if line.endswith("\n") or len(line) < read_limit:
            yield line
            continue
        while line and not line.endswith("\n"):
            line = stream.readline(read_limit)
        yield _OVERSIZED_RECORD


def _iter_bounded_byte_lines(
    stream: BinaryIO,
) -> Iterable[str | _OversizedRecord | _InvalidUtf8Record]:
    """Read stdin with the record cap applied to raw bytes before UTF-8 decode."""
    read_limit = _MAX_RECORD_BYTES + 1
    while True:
        line = stream.readline(read_limit)
        if line == b"":
            return
        if line.endswith(b"\n") or len(line) < read_limit:
            if len(line) > _MAX_RECORD_BYTES:
                yield _OVERSIZED_RECORD
                continue
            try:
                yield line.decode("utf-8")
            except UnicodeDecodeError:
                yield _INVALID_UTF8_RECORD
            continue
        while line and not line.endswith(b"\n"):
            line = stream.readline(read_limit)
        yield _OVERSIZED_RECORD


def inspect_lines(
    lines: Iterable[str | _OversizedRecord | _InvalidUtf8Record],
) -> dict[str, Any]:
    """Inspect JSONL records without returning any source content."""
    reasons: Counter[str] = Counter()
    records = 0
    invalid = False

    for line in lines:
        if line is _OVERSIZED_RECORD:
            records += 1
            reasons["RECORD_TOO_LARGE"] += 1
            invalid = True
            continue
        if line is _INVALID_UTF8_RECORD:
            records += 1
            reasons["INVALID_JSON"] += 1
            invalid = True
            continue
        try:
            record_bytes = len(line.encode("utf-8"))
        except UnicodeEncodeError:
            records += 1
            reasons["INVALID_JSON"] += 1
            invalid = True
            continue
        if record_bytes > _MAX_RECORD_BYTES:
            records += 1
            reasons["RECORD_TOO_LARGE"] += 1
            invalid = True
            continue
        if not line.strip():
            continue
        records += 1
        try:
            value = json.loads(
                line,
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_nonfinite,
            )
        except _DuplicateKeyError:
            reasons["DUPLICATE_JSON_KEY"] += 1
            invalid = True
            continue
        except _NonFiniteNumberError:
            reasons["NONFINITE_NUMBER"] += 1
            invalid = True
            continue
        except RecursionError:
            reasons["NESTING_TOO_DEEP"] += 1
            invalid = True
            continue
        except (json.JSONDecodeError, UnicodeError, TypeError, ValueError):
            reasons["INVALID_JSON"] += 1
            invalid = True
            continue
        if _inspect_record(value, reasons):
            invalid = True

    if invalid:
        status = "INVALID"
    elif reasons:
        status = "REVIEW_REQUIRED"
    else:
        status = "SAFE"
    return {
        "status": status,
        "records": records,
        "violations": dict(sorted(reasons.items())),
    }


def _exit_code(status: str) -> int:
    return {"SAFE": 0, "REVIEW_REQUIRED": 2, "INVALID": 3}[status]


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit JSONL structured logs for baseline schema and secret redaction."
    )
    parser.parse_args(argv)
    source = stdin if stdin is not None else sys.stdin
    raw_source = getattr(source, "buffer", None)
    lines = (
        _iter_bounded_byte_lines(raw_source)
        if raw_source is not None
        else _iter_bounded_lines(source)
    )
    result = inspect_lines(lines)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return _exit_code(result["status"])


if __name__ == "__main__":
    raise SystemExit(main())
