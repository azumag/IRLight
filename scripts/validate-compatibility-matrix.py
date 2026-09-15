#!/usr/bin/env python3
"""Validate the compatibility evidence matrix without external dependencies."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "docs" / "compatibility-matrix.json"
MANUAL_REPORT_PREFIX = "docs/compatibility-reports/"
ALLOWED_STATUSES = {"automated", "manual_verified", "not_tested"}
ALLOWED_CATEGORIES = {
    "publisher_software",
    "mobile_publisher",
    "hardware_publisher",
    "output_destination",
}
REQUIRED_ENTRY_FIELDS = {
    "id",
    "coverage",
    "category",
    "subject",
    "transport",
    "profile",
    "network",
    "status",
    "evidence",
    "notes",
}


def _string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _safe_repo_path(raw: str) -> Path | None:
    path = Path(raw)
    if path.is_absolute() or not raw or any(part == ".." for part in path.parts):
        return None
    candidate = (ROOT / path).resolve()
    try:
        candidate.relative_to(ROOT.resolve())
    except ValueError:
        return None
    return candidate


def _automated_evidence_path(raw: str) -> bool:
    path = Path(raw)
    suffix = path.suffix.lower()
    if raw.startswith(".github/workflows/"):
        return suffix in {".yml", ".yaml"}
    if raw.startswith("scripts/") or raw.startswith("tests/"):
        return suffix in {".sh", ".py"}
    return False


def validate_matrix(matrix: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if matrix.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    policy = matrix.get("generated_claims_policy")
    if not _string(policy):
        errors.append("generated_claims_policy must be a non-empty string")

    raw_required_coverage = matrix.get("required_coverage")
    required_coverage: list[str] = []
    if not isinstance(raw_required_coverage, list) or not raw_required_coverage:
        errors.append("required_coverage must be a non-empty list")
    else:
        if any(not _string(item) for item in raw_required_coverage):
            errors.append("required_coverage entries must be non-empty strings")
        required_coverage = [
            item for item in raw_required_coverage if _string(item)
        ]

    required_coverage_set = set(required_coverage)
    if len(required_coverage) != len(required_coverage_set):
        errors.append("required_coverage entries must be unique")

    entries = matrix.get("entries")
    if not isinstance(entries, list) or not entries:
        errors.append("entries must be a non-empty list")
        return errors

    ids: set[str] = set()
    covered: set[str] = set()
    coverage_statuses: dict[str, set[str]] = {}
    for index, entry in enumerate(entries):
        prefix = f"entries[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} must be an object")
            continue

        missing = sorted(REQUIRED_ENTRY_FIELDS - entry.keys())
        if missing:
            errors.append(f"{prefix} missing fields: {', '.join(missing)}")
            continue

        entry_id = entry.get("id")
        if not _string(entry_id):
            errors.append(f"{prefix}.id must be a non-empty string")
        elif entry_id in ids:
            errors.append(f"duplicate entry id: {entry_id}")
        else:
            ids.add(entry_id)

        coverage = entry.get("coverage")
        if not _string(coverage):
            errors.append(f"{prefix}.coverage must be a non-empty string")
            valid_coverage = None
        else:
            valid_coverage = coverage
            covered.add(coverage)

        for field in ("subject", "transport", "profile", "network", "notes"):
            if not _string(entry.get(field)):
                errors.append(f"{prefix}.{field} must be a non-empty string")

        category = entry.get("category")
        if category not in ALLOWED_CATEGORIES:
            errors.append(
                f"{prefix}.category must be one of {sorted(ALLOWED_CATEGORIES)}"
            )

        status = entry.get("status")
        if status not in ALLOWED_STATUSES:
            errors.append(f"{prefix}.status must be one of {sorted(ALLOWED_STATUSES)}")
        elif valid_coverage is not None:
            coverage_statuses.setdefault(valid_coverage, set()).add(status)

        evidence = entry.get("evidence")
        if not isinstance(evidence, list) or any(not _string(item) for item in evidence):
            errors.append(f"{prefix}.evidence must be a list of non-empty strings")
            continue

        if status == "not_tested" and evidence:
            errors.append(f"{prefix}: not_tested entries must not carry evidence")
        if status in {"automated", "manual_verified"} and not evidence:
            errors.append(f"{prefix}: verified entries require evidence")
        if status == "automated":
            for raw_path in evidence:
                if not _automated_evidence_path(raw_path):
                    errors.append(
                        f"{prefix}: automated evidence must be a workflow (.yml/.yaml) "
                        "or test script (.sh/.py) under .github/workflows/, scripts/, or tests/"
                    )
        if status == "manual_verified":
            for raw_path in evidence:
                if not raw_path.startswith(MANUAL_REPORT_PREFIX):
                    errors.append(
                        f"{prefix}: manual_verified evidence must live under "
                        f"{MANUAL_REPORT_PREFIX}"
                    )

        for raw_path in evidence:
            path = _safe_repo_path(raw_path)
            if path is None:
                errors.append(f"{prefix}.evidence contains unsafe path: {raw_path!r}")
            elif not path.is_file():
                errors.append(f"{prefix}.evidence does not exist: {raw_path}")

    missing_coverage = sorted(required_coverage_set - covered)
    if missing_coverage:
        errors.append(
            "entries do not cover required_coverage: " + ", ".join(missing_coverage)
        )

    unexpected_coverage = sorted(covered - required_coverage_set)
    if unexpected_coverage:
        errors.append(
            "entries use coverage keys not declared in required_coverage: "
            + ", ".join(unexpected_coverage)
        )

    for coverage, statuses in sorted(coverage_statuses.items()):
        if "not_tested" in statuses and statuses & {"automated", "manual_verified"}:
            errors.append(
                f"coverage {coverage} cannot be both not_tested and verified"
            )

    return errors


def load_matrix(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read compatibility matrix: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("compatibility matrix root must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_MATRIX)
    args = parser.parse_args()

    try:
        matrix = load_matrix(args.path)
    except ValueError as exc:
        print(f"compatibility matrix invalid: {exc}", file=sys.stderr)
        return 1

    errors = validate_matrix(matrix)
    if errors:
        for error in errors:
            print(f"compatibility matrix invalid: {error}", file=sys.stderr)
        return 1

    print(
        "compatibility matrix valid: "
        f"{len(matrix['entries'])} entries, "
        f"{len(matrix['required_coverage'])} required coverage keys"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
