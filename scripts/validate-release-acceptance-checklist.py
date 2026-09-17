#!/usr/bin/env python3
"""Validate the fail-closed IRLight pre-beta release acceptance checklist."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHECKLIST = ROOT / "docs" / "release-acceptance-checklist.json"
CAPACITY_REPORT_VALIDATOR = ROOT / "scripts" / "validate-node-capacity-report.py"
MAX_CHECKLIST_BYTES = 128 * 1024

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


def load_checklist(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_CHECKLIST_BYTES:
            raise ChecklistValidationError("checklist exceeds size limit")
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ChecklistValidationError("checklist could not be read") from exc

    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except json.JSONDecodeError as exc:
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


def _load_capacity_report_validator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "irlight_release_capacity_report_validator",
        CAPACITY_REPORT_VALIDATOR,
    )
    if spec is None or spec.loader is None:
        raise ChecklistValidationError("Node capacity report validator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError) as exc:
        raise ChecklistValidationError(
            "Node capacity report validator could not be loaded"
        ) from exc
    return module


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
