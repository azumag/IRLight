#!/usr/bin/env python3
"""Validate the fail-closed IRLight pre-beta release acceptance checklist.

The canonical checklist is a bounded regular-file input. The loader rejects
symlinks and non-regular files, opens without following the final pathname,
and verifies that both the opened file metadata and pathname identity remain
stable across the bounded read.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHECKLIST = ROOT / "docs" / "release-acceptance-checklist.json"
SOAK_REPORT_VALIDATOR = ROOT / "scripts" / "validate-soak-report.py"
CAPACITY_REPORT_VALIDATOR = ROOT / "scripts" / "validate-node-capacity-report.py"
MAX_CHECKLIST_BYTES = 128 * 1024
MIN_SIX_HOUR_SOAK_SECONDS = 6 * 60 * 60

ALLOWED_STATUSES = {"pending", "partial", "blocked", "satisfied"}
REQUIRED_ITEM_IDS = {
    "major-transport-flows",
    "disconnect-recovery",
    "obs-compatibility",
    "mobile-publisher-compatibility",
    "hardware-encoder-compatibility",
    "six-hour-soak",
    "node-capacity-load",
    "beta-release-checklist",
}
TOP_LEVEL_FIELDS = {"schema_version", "qa_acceptance_ready", "policy", "items"}
ITEM_FIELDS = {"id", "title", "required", "status", "evidence", "notes"}


class ChecklistValidationError(ValueError):
    """The release acceptance checklist is invalid or overclaims readiness."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ChecklistValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise ChecklistValidationError(f"non-standard JSON constant: {value}")


def _open_checklist_readonly(path: Path) -> Any:
    """Open one stable regular-file checklist without following a final symlink."""

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ChecklistValidationError("checklist could not be inspected") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ChecklistValidationError("checklist must be a regular file")
    if before.st_size > MAX_CHECKLIST_BYTES:
        raise ChecklistValidationError("checklist exceeds size limit")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ChecklistValidationError("checklist could not be opened") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ChecklistValidationError("checklist must be a regular file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ChecklistValidationError("checklist changed while opening")
        if opened.st_size > MAX_CHECKLIST_BYTES:
            raise ChecklistValidationError("checklist exceeds size limit")
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def load_checklist(path: Path) -> dict[str, Any]:
    try:
        with _open_checklist_readonly(path) as handle:
            before_read = os.fstat(handle.fileno())
            raw_bytes = handle.read(MAX_CHECKLIST_BYTES + 1)
            after_read = os.fstat(handle.fileno())
            if _file_identity(before_read) != _file_identity(after_read):
                raise ChecklistValidationError("checklist changed while reading")
            current = os.lstat(path)
            if not stat.S_ISREG(current.st_mode) or (
                current.st_dev,
                current.st_ino,
            ) != (after_read.st_dev, after_read.st_ino):
                raise ChecklistValidationError("checklist changed while reading")
        if len(raw_bytes) > MAX_CHECKLIST_BYTES:
            raise ChecklistValidationError("checklist exceeds size limit")
        raw = raw_bytes.decode("utf-8")
    except ChecklistValidationError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise ChecklistValidationError("checklist could not be read") from exc

    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ChecklistValidationError("checklist is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ChecklistValidationError("checklist root must be an object")
    return payload


def _validate_repo_evidence(path_text: object) -> str:
    if not isinstance(path_text, str) or not path_text or path_text != path_text.strip():
        raise ChecklistValidationError("evidence paths must be canonical non-empty strings")
    path = Path(path_text)
    if path.is_absolute() or ".." in path.parts:
        raise ChecklistValidationError("evidence paths must stay inside the repository")
    candidate = ROOT / path
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(ROOT.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise ChecklistValidationError(f"evidence path is missing or unsafe: {path_text}") from exc
    if not resolved.is_file():
        raise ChecklistValidationError(f"evidence path must be a file: {path_text}")
    return path_text


def _load_report_validator(path: Path, module_name: str, label: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ChecklistValidationError(f"{label} validator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError) as exc:
        raise ChecklistValidationError(f"{label} validator could not be loaded") from exc
    return module


def _load_soak_report_validator() -> Any:
    return _load_report_validator(
        SOAK_REPORT_VALIDATOR,
        "irlight_release_soak_report_validator",
        "six-hour soak report",
    )


def _load_capacity_report_validator() -> Any:
    return _load_report_validator(
        CAPACITY_REPORT_VALIDATOR,
        "irlight_release_capacity_report_validator",
        "Node capacity report",
    )


def _validate_six_hour_soak_evidence(evidence_paths: list[str]) -> None:
    """Require a canonical passing six-hour-or-longer soak report."""

    validator = _load_soak_report_validator()
    failures: list[str] = []
    for path_text in evidence_paths:
        candidate = ROOT / path_text
        try:
            summary = validator.validate_report(validator.load_report(candidate))
        except (validator.SoakReportError, OSError, UnicodeError) as exc:
            failures.append(f"{path_text}: {exc}")
            continue

        if summary["outcome"] != "pass":
            failures.append(f"{path_text}: outcome must be pass")
            continue
        if summary["target_duration_seconds"] < MIN_SIX_HOUR_SOAK_SECONDS:
            failures.append(
                f"{path_text}: target_duration_seconds must be >= {MIN_SIX_HOUR_SOAK_SECONDS}"
            )
            continue
        if summary["observed_duration_seconds"] < MIN_SIX_HOUR_SOAK_SECONDS:
            failures.append(
                f"{path_text}: observed_duration_seconds must be >= {MIN_SIX_HOUR_SOAK_SECONDS}"
            )
            continue
        return

    detail = "; ".join(failures[:3])
    if detail:
        detail = f" ({detail})"
    raise ChecklistValidationError(
        "six-hour-soak: satisfied status requires at least one canonical passing "
        f"six-hour-or-longer soak report{detail}"
    )


def _validate_node_capacity_evidence(evidence_paths: list[str]) -> None:
    """Require a canonical capacity report before capacity can be marked satisfied."""

    validator = _load_capacity_report_validator()
    failures: list[str] = []
    for path_text in evidence_paths:
        candidate = ROOT / path_text
        try:
            validator.validate_report(validator.load_report(candidate))
        except (validator.CapacityReportError, OSError, UnicodeError) as exc:
            failures.append(f"{path_text}: {exc}")
            continue
        return

    detail = "; ".join(failures[:3])
    if detail:
        detail = f" ({detail})"
    raise ChecklistValidationError(
        "node-capacity-load: satisfied status requires at least one canonical "
        f"Node capacity report{detail}"
    )


def validate_checklist(payload: dict[str, Any]) -> None:
    if set(payload) != TOP_LEVEL_FIELDS:
        raise ChecklistValidationError("checklist has an unexpected top-level shape")
    schema_version = payload["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1:
        raise ChecklistValidationError("unsupported checklist schema_version")
    if not isinstance(payload["qa_acceptance_ready"], bool):
        raise ChecklistValidationError("qa_acceptance_ready must be boolean")
    if (
        not isinstance(payload["policy"], str)
        or not payload["policy"].strip()
        or payload["policy"] != payload["policy"].strip()
    ):
        raise ChecklistValidationError("policy must be a canonical non-empty string")
    items = payload["items"]
    if not isinstance(items, list) or not items:
        raise ChecklistValidationError("items must be a non-empty list")

    seen_ids: set[str] = set()
    required_statuses: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != ITEM_FIELDS:
            raise ChecklistValidationError("checklist item has an unexpected shape")

        item_id = item["id"]
        if (
            not isinstance(item_id, str)
            or not item_id
            or item_id != item_id.strip()
            or item_id in seen_ids
        ):
            raise ChecklistValidationError("item ids must be unique canonical strings")
        seen_ids.add(item_id)

        for field in ("title", "notes"):
            value = item[field]
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ChecklistValidationError(
                    f"{item_id}: {field} must be a canonical non-empty string"
                )

        if not isinstance(item["required"], bool):
            raise ChecklistValidationError(f"{item_id}: required must be boolean")
        status = item["status"]
        if not isinstance(status, str) or status not in ALLOWED_STATUSES:
            raise ChecklistValidationError(f"{item_id}: unsupported status")

        evidence = item["evidence"]
        if not isinstance(evidence, list):
            raise ChecklistValidationError(f"{item_id}: evidence must be a list")
        normalized_evidence = [_validate_repo_evidence(entry) for entry in evidence]
        if len(normalized_evidence) != len(set(normalized_evidence)):
            raise ChecklistValidationError(f"{item_id}: duplicate evidence path")
        if status == "satisfied" and not normalized_evidence:
            raise ChecklistValidationError(
                f"{item_id}: satisfied items require repository evidence"
            )
        if item_id == "six-hour-soak" and status == "satisfied":
            _validate_six_hour_soak_evidence(normalized_evidence)
        if item_id == "node-capacity-load" and status == "satisfied":
            _validate_node_capacity_evidence(normalized_evidence)

        if item["required"]:
            required_statuses[item_id] = status

    missing_required = REQUIRED_ITEM_IDS - required_statuses.keys()
    if missing_required:
        raise ChecklistValidationError(
            "missing required acceptance items: " + ", ".join(sorted(missing_required))
        )

    unexpected_required = required_statuses.keys() - REQUIRED_ITEM_IDS
    if unexpected_required:
        raise ChecklistValidationError(
            "unexpected required acceptance items: "
            + ", ".join(sorted(unexpected_required))
        )

    all_required_satisfied = all(
        status == "satisfied" for status in required_statuses.values()
    )
    if payload["qa_acceptance_ready"] != all_required_satisfied:
        raise ChecklistValidationError(
            "qa_acceptance_ready must exactly reflect all required items being satisfied"
        )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else DEFAULT_CHECKLIST
    try:
        payload = load_checklist(path)
        validate_checklist(payload)
    except ChecklistValidationError as exc:
        print(f"release-acceptance-checklist: {exc}", file=sys.stderr)
        return 1

    print("release-acceptance-checklist: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
