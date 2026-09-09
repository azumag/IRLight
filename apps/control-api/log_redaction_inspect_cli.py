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
from typing import Any, TextIO
from urllib.parse import parse_qsl, urlsplit

_REQUIRED_FIELDS = ("timestamp", "level", "service", "event_type")
_REDACTED_VALUES = {"[redacted]", "<redacted>", "***"}
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SEPARATORS = re.compile(r"[^0-9A-Za-z]+")
_SENSITIVE_KEYS = {
    "secret",
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
    "private_key",
    "client_secret",
    "cookie",
    "set_cookie",
}


class _DuplicateKeyError(ValueError):
    pass


class _NonFiniteNumberError(ValueError):
    pass


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
    expanded = _CAMEL_CASE_BOUNDARY.sub("_", key.strip())
    return _KEY_SEPARATORS.sub("_", expanded).strip("_").lower()


def _is_redacted(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and value.strip().lower() in _REDACTED_VALUES


def _url_for_secret_scan(value: str):
    candidate = value.strip()
    if "://" not in candidate and not candidate.startswith(("/", "./", "../", "?")):
        return None
    try:
        return urlsplit(candidate)
    except ValueError:
        return None


def _url_has_unredacted_sensitive_query(value: str) -> bool:
    parsed = _url_for_secret_scan(value)
    if parsed is None:
        return False
    try:
        params = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        return False
    for key, raw_value in params:
        if _normalize_key(key) in _SENSITIVE_KEYS and not _is_redacted(raw_value):
            return True
    return False


def _url_has_userinfo(value: str) -> bool:
    parsed = _url_for_secret_scan(value)
    if parsed is None or "://" not in value:
        return False
    try:
        return parsed.username is not None or parsed.password is not None
    except ValueError:
        return False


def _scan_value(value: Any, reasons: Counter[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = _normalize_key(str(key))
            if normalized in _SENSITIVE_KEYS and not _is_redacted(child):
                reasons["SENSITIVE_FIELD_UNREDACTED"] += 1
            _scan_value(child, reasons)
        return
    if isinstance(value, list):
        for child in value:
            _scan_value(child, reasons)
        return
    if isinstance(value, str):
        if _url_has_unredacted_sensitive_query(value):
            reasons["SENSITIVE_URL_QUERY_UNREDACTED"] += 1
        if _url_has_userinfo(value):
            reasons["SENSITIVE_URL_USERINFO"] += 1


def _inspect_record(record: Any, reasons: Counter[str]) -> None:
    if not isinstance(record, dict):
        reasons["INVALID_RECORD"] += 1
        return
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
    _scan_value(record, reasons)


def inspect_lines(lines: Iterable[str]) -> dict[str, Any]:
    """Inspect JSONL records without returning any source content."""
    reasons: Counter[str] = Counter()
    records = 0
    invalid = False

    for line in lines:
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
        except (json.JSONDecodeError, UnicodeError, TypeError, ValueError):
            reasons["INVALID_JSON"] += 1
            invalid = True
            continue
        before_invalid = reasons["INVALID_RECORD"]
        _inspect_record(value, reasons)
        if reasons["INVALID_RECORD"] > before_invalid:
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
    result = inspect_lines(stdin if stdin is not None else sys.stdin)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return _exit_code(result["status"])


if __name__ == "__main__":
    raise SystemExit(main())
