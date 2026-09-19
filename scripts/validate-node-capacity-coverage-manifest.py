#!/usr/bin/env python3
"""Validate durable repository evidence for complete Node-capacity scenario coverage."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_VALIDATOR = Path(__file__).with_name("validate-node-capacity-plan-coverage.py")
MAX_MANIFEST_BYTES = 128 * 1024
TOP_LEVEL_FIELDS = {"schema_version", "load_plan", "reports"}
REPORT_FIELDS = {"scenario_id", "path"}


class CapacityCoverageManifestError(ValueError):
    """Raised when durable Node-capacity coverage evidence is unsafe or incomplete."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapacityCoverageManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise CapacityCoverageManifestError(f"non-standard JSON constant: {value}")


def _identity(snapshot: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        snapshot.st_dev,
        snapshot.st_ino,
        snapshot.st_size,
        snapshot.st_mtime_ns,
        snapshot.st_ctime_ns,
    )


def _read_manifest_bytes(path: Path) -> bytes:
    """Read one bounded stable regular file without following a final symlink."""

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise CapacityCoverageManifestError("manifest could not be inspected") from exc
    if not stat.S_ISREG(before.st_mode):
        raise CapacityCoverageManifestError("manifest must be a regular file")
    if before.st_size > MAX_MANIFEST_BYTES:
        raise CapacityCoverageManifestError("manifest exceeds size limit")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CapacityCoverageManifestError("manifest could not be opened") from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise CapacityCoverageManifestError("manifest must be a regular file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise CapacityCoverageManifestError("manifest changed while opening")
        if opened.st_size > MAX_MANIFEST_BYTES:
            raise CapacityCoverageManifestError("manifest exceeds size limit")

        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise CapacityCoverageManifestError("manifest exceeds size limit")

        after_read = os.fstat(fd)
        try:
            after_path = os.lstat(path)
        except OSError as exc:
            raise CapacityCoverageManifestError("manifest changed while reading") from exc
        if not stat.S_ISREG(after_path.st_mode):
            raise CapacityCoverageManifestError("manifest changed while reading")
        if _identity(opened) != _identity(after_read) or _identity(opened) != _identity(after_path):
            raise CapacityCoverageManifestError("manifest changed while reading")
        return raw
    finally:
        os.close(fd)


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = _read_manifest_bytes(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CapacityCoverageManifestError("manifest must be valid UTF-8") from exc

    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except CapacityCoverageManifestError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise CapacityCoverageManifestError("manifest is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CapacityCoverageManifestError("manifest root must be an object")
    return payload


def _load_coverage_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "irlight_node_capacity_coverage_for_manifest", COVERAGE_VALIDATOR
    )
    if spec is None or spec.loader is None:
        raise CapacityCoverageManifestError("coverage validator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise CapacityCoverageManifestError("coverage validator could not be loaded") from exc
    return module


def _validate_repo_file(repo_root: Path, path_text: object, label: str) -> Path:
    if not isinstance(path_text, str) or not path_text or path_text != path_text.strip():
        raise CapacityCoverageManifestError(f"{label} must be a canonical repository path")
    relative = Path(path_text)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "." in relative.parts
        or relative.as_posix() != path_text
    ):
        raise CapacityCoverageManifestError(f"{label} must stay inside the repository")

    try:
        root = repo_root.resolve(strict=True)
        candidate = repo_root / relative
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CapacityCoverageManifestError(f"{label} is missing or unsafe") from exc

    current = repo_root
    try:
        for part in relative.parts:
            current = current / part
            if os.path.islink(current):
                raise CapacityCoverageManifestError(f"{label} must not traverse symlinks")
        snapshot = os.lstat(candidate)
    except CapacityCoverageManifestError:
        raise
    except OSError as exc:
        raise CapacityCoverageManifestError(f"{label} is missing or unsafe") from exc
    if not stat.S_ISREG(snapshot.st_mode):
        raise CapacityCoverageManifestError(f"{label} must be a regular file")
    return candidate


def validate_manifest(
    payload: dict[str, Any],
    *,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    if set(payload) != TOP_LEVEL_FIELDS:
        raise CapacityCoverageManifestError("manifest has an unexpected top-level shape")

    schema_version = payload["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1:
        raise CapacityCoverageManifestError("unsupported manifest schema_version")

    plan_path = _validate_repo_file(repo_root, payload["load_plan"], "load_plan")
    reports = payload["reports"]
    if not isinstance(reports, list) or not reports:
        raise CapacityCoverageManifestError("reports must be a non-empty list")

    bindings: dict[str, Path] = {}
    for entry in reports:
        if not isinstance(entry, dict) or set(entry) != REPORT_FIELDS:
            raise CapacityCoverageManifestError("report binding has an unexpected shape")
        scenario_id = entry["scenario_id"]
        if (
            not isinstance(scenario_id, str)
            or not scenario_id
            or scenario_id != scenario_id.strip()
            or any(character in scenario_id for character in ("\x00", "\n", "\r"))
        ):
            raise CapacityCoverageManifestError("scenario_id must be a canonical one-line string")
        if scenario_id in bindings:
            raise CapacityCoverageManifestError(f"duplicate report binding for scenario: {scenario_id}")
        bindings[scenario_id] = _validate_repo_file(
            repo_root, entry["path"], f"report path for {scenario_id}"
        )

    coverage_validator = _load_coverage_validator()
    try:
        coverage = coverage_validator.validate_coverage(plan_path, bindings)
    except coverage_validator.CapacityCoverageError as exc:
        raise CapacityCoverageManifestError("scenario coverage is invalid") from exc

    return {
        "valid": True,
        "schema_version": 1,
        "load_plan": payload["load_plan"],
        "profile_label": coverage["profile_label"],
        "node_profile": coverage["node_profile"],
        "software_revision": coverage["software_revision"],
        "safety_margin_percent": coverage["safety_margin_percent"],
        "scenario_ids": [
            result["scenario_id"] for result in coverage["scenario_results"]
        ],
        "report_count": len(coverage["scenario_results"]),
        "recommended_max_sessions": coverage["recommended_max_sessions"],
    }


def validate_manifest_file(
    path: Path,
    *,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    return validate_manifest(load_manifest(path), repo_root=repo_root)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="repository Node-capacity coverage manifest")
    parser.add_argument("--json", action="store_true", help="emit a deterministic JSON summary")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = validate_manifest_file(args.manifest)
    except (CapacityCoverageManifestError, OSError) as exc:
        print(f"node capacity coverage manifest invalid: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(summary, sys.stdout, ensure_ascii=False, sort_keys=True, allow_nan=False)
        sys.stdout.write("\n")
    else:
        print(
            "node capacity coverage manifest valid: "
            f"scenarios={summary['report_count']} "
            f"recommended_max_sessions={summary['recommended_max_sessions']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
