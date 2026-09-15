#!/usr/bin/env python3
"""Validate manual compatibility reports referenced by the evidence matrix."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "docs" / "compatibility-matrix.json"
MANUAL_REPORT_PREFIX = "docs/compatibility-reports/"
MAX_MANUAL_REPORT_BYTES = 64 * 1024
MAX_COMPATIBILITY_MATRIX_BYTES = 256 * 1024
REPORT_RESULTS = {"PASS", "PARTIAL", "FAIL", "BLOCKED"}
CHECK_RESULTS = {"PASS", "FAIL", "BLOCKED", "NOT_APPLICABLE"}
REQUIRED_REPORT_FIELDS = {
    "schema_version",
    "entry_id",
    "coverage",
    "tested_at",
    "subject",
    "version",
    "environment",
    "transport",
    "profile",
    "network",
    "result",
    "checks",
    "notes",
}
ALLOWED_CHECK_FIELDS = {"name", "result", "notes"}
SENSITIVE_FIELD_NAMES = {
    "apikey",
    "accesstoken",
    "authorization",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "cookie",
    "credential",
    "passphrase",
    "password",
    "privatekey",
    "secret",
    "sessiontoken",
    "streamkey",
    "token",
}
SENSITIVE_URL_QUERY_NAMES = SENSITIVE_FIELD_NAMES | {"streamid"}
URL_CANDIDATE_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>'\"`]+")


def _string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _timezone_aware_iso8601(value: Any) -> bool:
    if not _string(value):
        return False
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _normalize_sensitive_name(value: Any) -> str:
    return "".join(character for character in str(value).strip().lower() if character.isalnum())


def _sensitive_field_paths(value: Any, *, prefix: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            if _normalize_sensitive_name(key) in SENSITIVE_FIELD_NAMES:
                findings.append(path)
            findings.extend(_sensitive_field_paths(child, prefix=path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            findings.extend(_sensitive_field_paths(child, prefix=path))
    return findings


def _credential_url_paths(value: Any, *, prefix: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            findings.extend(_credential_url_paths(child, prefix=path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            findings.extend(_credential_url_paths(child, prefix=path))
    elif isinstance(value, str):
        for raw_candidate in URL_CANDIDATE_RE.findall(value):
            candidate = raw_candidate.rstrip(".,);]}")
            try:
                parsed = urlsplit(candidate)
            except ValueError:
                continue
            if not parsed.netloc:
                continue
            if parsed.username is not None or parsed.password is not None:
                findings.append(prefix or "<root>")
                continue
            try:
                query_fields = parse_qsl(parsed.query, keep_blank_values=True)
            except ValueError:
                continue
            if any(
                _normalize_sensitive_name(name) in SENSITIVE_URL_QUERY_NAMES
                for name, _ in query_fields
            ):
                findings.append(prefix or "<root>")
    return findings


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("duplicate object key is not allowed")
        value[key] = child
    return value


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _load_strict_json_object(
    path: Path,
    *,
    description: str,
    max_bytes: int = MAX_MANUAL_REPORT_BYTES,
) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot stat {description}: {exc}") from exc
    if size > max_bytes:
        raise ValueError(f"{description} exceeds {max_bytes}-byte limit")

    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"cannot read {description}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{description} root must be an object")
    return value


def load_compatibility_matrix(path: Path) -> dict[str, Any]:
    """Load the shared compatibility ledger through the same strict JSON boundary."""

    return _load_strict_json_object(
        path,
        description="compatibility matrix",
        max_bytes=MAX_COMPATIBILITY_MATRIX_BYTES,
    )


def validate_report(
    report: dict[str, Any],
    *,
    expected_entry_id: str | None = None,
    expected_coverage: str | None = None,
    require_pass: bool = False,
) -> list[str]:
    errors: list[str] = []

    schema_version = report.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        errors.append("schema_version must be integer 1")

    missing = sorted(REQUIRED_REPORT_FIELDS - report.keys())
    if missing:
        errors.append("missing fields: " + ", ".join(missing))
    unknown = sorted(
        str(field) for field in report.keys() if field not in REQUIRED_REPORT_FIELDS
    )
    if unknown:
        errors.append("unknown fields are not allowed: " + ", ".join(unknown))

    for field in (
        "entry_id",
        "coverage",
        "subject",
        "version",
        "environment",
        "transport",
        "profile",
        "network",
    ):
        if field in report and not _string(report.get(field)):
            errors.append(f"{field} must be a non-empty string")

    if not _timezone_aware_iso8601(report.get("tested_at")):
        errors.append("tested_at must be a timezone-aware ISO 8601 timestamp")

    result = report.get("result")
    if not isinstance(result, str) or result not in REPORT_RESULTS:
        errors.append(f"result must be one of {sorted(REPORT_RESULTS)}")
    elif require_pass and result != "PASS":
        errors.append("manual_verified evidence must have result=PASS")

    notes = report.get("notes")
    if not isinstance(notes, str):
        errors.append("notes must be a string")

    checks = report.get("checks")
    valid_check_results: list[str] = []
    if not isinstance(checks, list) or not checks:
        errors.append("checks must be a non-empty list")
    else:
        for index, check in enumerate(checks):
            prefix = f"checks[{index}]"
            if not isinstance(check, dict):
                errors.append(f"{prefix} must be an object")
                continue
            unknown_check_fields = sorted(
                str(field) for field in check.keys() if field not in ALLOWED_CHECK_FIELDS
            )
            if unknown_check_fields:
                errors.append(
                    f"{prefix} has unknown fields: " + ", ".join(unknown_check_fields)
                )
            if not _string(check.get("name")):
                errors.append(f"{prefix}.name must be a non-empty string")
            check_result = check.get("result")
            if not isinstance(check_result, str) or check_result not in CHECK_RESULTS:
                errors.append(
                    f"{prefix}.result must be one of {sorted(CHECK_RESULTS)}"
                )
            else:
                valid_check_results.append(check_result)
                if require_pass and check_result not in {"PASS", "NOT_APPLICABLE"}:
                    errors.append(
                        f"{prefix}.result must be PASS or NOT_APPLICABLE for "
                        "manual_verified evidence"
                    )
            if "notes" in check and not isinstance(check.get("notes"), str):
                errors.append(f"{prefix}.notes must be a string when present")

    if require_pass and valid_check_results and "PASS" not in valid_check_results:
        errors.append("manual_verified evidence must include at least one PASS check")

    entry_id = report.get("entry_id")
    coverage = report.get("coverage")
    if expected_entry_id is not None and entry_id != expected_entry_id:
        errors.append(f"entry_id must match matrix entry {expected_entry_id!r}")
    if expected_coverage is not None and coverage != expected_coverage:
        errors.append(f"coverage must match matrix coverage {expected_coverage!r}")

    for path in sorted(set(_sensitive_field_paths(report))):
        errors.append(f"sensitive field name is not allowed in evidence: {path}")
    for path in sorted(set(_credential_url_paths(report))):
        errors.append(f"credential-bearing URL is not allowed in evidence: {path}")

    return errors


def _safe_report_path(root: Path, raw_path: str) -> Path | None:
    if not raw_path.startswith(MANUAL_REPORT_PREFIX):
        return None
    relative = Path(raw_path)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        return None
    root_resolved = root.resolve()
    report_root = root_resolved / MANUAL_REPORT_PREFIX.rstrip("/")
    candidate = (root_resolved / relative).resolve()
    try:
        candidate.relative_to(report_root)
    except ValueError:
        return None
    return candidate


def validate_matrix_manual_reports(
    matrix: dict[str, Any], *, root: Path = ROOT
) -> list[str]:
    errors: list[str] = []
    entries = matrix.get("entries")
    if not isinstance(entries, list):
        return ["matrix entries must be a list"]

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("status") != "manual_verified":
            continue

        prefix = f"entries[{index}]"
        entry_id = entry.get("id")
        coverage = entry.get("coverage")
        if not _string(entry_id):
            errors.append(f"{prefix}.id must be a non-empty string")
            continue
        if not _string(coverage):
            errors.append(f"{prefix}.coverage must be a non-empty string")
            continue

        evidence = entry.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{prefix}: manual_verified entries require evidence")
            continue

        for raw_path in evidence:
            if not _string(raw_path):
                errors.append(f"{prefix}.evidence must contain non-empty strings")
                continue
            if not raw_path.endswith(".json"):
                errors.append(
                    f"{prefix}: manual compatibility evidence must be a JSON report: "
                    f"{raw_path}"
                )
                continue

            path = _safe_report_path(root, raw_path)
            if path is None:
                errors.append(f"{prefix}: unsafe manual evidence path: {raw_path}")
                continue
            try:
                report = _load_strict_json_object(
                    path, description=f"manual compatibility report {raw_path}"
                )
            except ValueError as exc:
                errors.append(f"{prefix}: {exc}")
                continue

            for error in validate_report(
                report,
                expected_entry_id=entry_id,
                expected_coverage=coverage,
                require_pass=True,
            ):
                errors.append(f"{raw_path}: {error}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matrix", nargs="?", type=Path, default=DEFAULT_MATRIX)
    args = parser.parse_args()

    try:
        matrix = load_compatibility_matrix(args.matrix)
    except ValueError as exc:
        print(f"manual compatibility evidence invalid: {exc}", file=sys.stderr)
        return 1

    errors = validate_matrix_manual_reports(matrix)
    if errors:
        for error in errors:
            print(f"manual compatibility evidence invalid: {error}", file=sys.stderr)
        return 1

    count = sum(
        1
        for entry in matrix.get("entries", [])
        if isinstance(entry, dict) and entry.get("status") == "manual_verified"
    )
    print(f"manual compatibility evidence valid: {count} verified entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
