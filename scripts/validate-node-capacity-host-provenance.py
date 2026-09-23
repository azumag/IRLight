#!/usr/bin/env python3
"""Validate a digest-pinned Node-capacity host-provenance closure.

This sidecar is additive: existing coverage schema v1/v2 remains unchanged. A
host-provenance manifest binds one validated coverage-v2 manifest, its canonical
load plan, every report/raw-trials/run-manifest input, and the exact validated
host-preflight evidence associated with each measured scenario.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_VALIDATOR = Path(__file__).with_name("validate-node-capacity-coverage-manifest.py")
PREFLIGHT_VALIDATOR = Path(__file__).with_name("validate-node-capacity-host-preflight.py")
MAX_MANIFEST_BYTES = 256 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
TOP_LEVEL_FIELDS = {"schema_version", "coverage", "load_plan", "scenarios"}
PIN_FIELDS = {"path", "sha256"}
SCENARIO_FIELDS = {
    "scenario_id",
    "host_preflight",
    "report",
    "trials",
    "run_manifest",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class HostProvenanceError(ValueError):
    """Raised when host provenance evidence is malformed, unsafe, or stale."""


def _load_module(path: Path, module_name: str, label: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise HostProvenanceError(f"{label} validator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise HostProvenanceError(f"{label} validator could not be loaded") from exc
    return module


def _coverage_validator() -> ModuleType:
    return _load_module(
        COVERAGE_VALIDATOR,
        "irlight_node_capacity_host_provenance_coverage_validator",
        "coverage",
    )


def _preflight_validator() -> ModuleType:
    return _load_module(
        PREFLIGHT_VALIDATOR,
        "irlight_node_capacity_host_provenance_preflight_validator",
        "host preflight",
    )


def _reject_constant(_value: str) -> None:
    raise HostProvenanceError("host provenance contains a non-standard JSON number")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HostProvenanceError("host provenance contains a duplicate JSON key")
        result[key] = value
    return result


def _identity(snapshot: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        snapshot.st_dev,
        snapshot.st_ino,
        snapshot.st_size,
        snapshot.st_mtime_ns,
        snapshot.st_ctime_ns,
    )


def _read_stable_bytes(path: Path, *, max_bytes: int, label: str) -> bytes:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise HostProvenanceError(f"{label} could not be inspected") from exc
    if not stat.S_ISREG(before.st_mode):
        raise HostProvenanceError(f"{label} must be a regular file")
    if before.st_size > max_bytes:
        raise HostProvenanceError(f"{label} exceeds maximum size")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise HostProvenanceError(f"{label} could not be opened") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise HostProvenanceError(f"{label} must be a regular file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise HostProvenanceError(f"{label} changed while opening")
        if opened.st_size > max_bytes:
            raise HostProvenanceError(f"{label} exceeds maximum size")

        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise HostProvenanceError(f"{label} exceeds maximum size")

        after_read = os.fstat(fd)
        try:
            after_path = os.lstat(path)
        except OSError as exc:
            raise HostProvenanceError(f"{label} changed while reading") from exc
        if not stat.S_ISREG(after_path.st_mode):
            raise HostProvenanceError(f"{label} changed while reading")
        if _identity(opened) != _identity(after_read) or _identity(opened) != _identity(after_path):
            raise HostProvenanceError(f"{label} changed while reading")
        return raw
    finally:
        os.close(fd)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = _read_stable_bytes(path, max_bytes=MAX_MANIFEST_BYTES, label="host provenance manifest")
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HostProvenanceError("host provenance manifest must be valid UTF-8") from exc
    try:
        value = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_strict_object)
    except HostProvenanceError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise HostProvenanceError("host provenance manifest must be valid JSON") from exc
    if not isinstance(value, dict):
        raise HostProvenanceError("host provenance manifest root must be an object")
    return value


def _validated_repo_path(
    repo_root: Path,
    path_text: object,
    *,
    label: str,
    coverage_validator: ModuleType,
) -> tuple[str, Path]:
    try:
        path = coverage_validator._validate_repo_file(repo_root, path_text, label)
    except coverage_validator.CapacityCoverageManifestError as exc:
        raise HostProvenanceError(f"{label} is missing or unsafe") from exc
    assert isinstance(path_text, str)
    return path_text, path


def _stable_digest(path: Path, *, label: str) -> str:
    return _sha256_bytes(
        _read_stable_bytes(path, max_bytes=MAX_ARTIFACT_BYTES, label=label)
    )


def make_pin(
    repo_root: Path,
    path_text: str,
    *,
    label: str,
    coverage_validator: ModuleType | None = None,
) -> dict[str, str]:
    validator = coverage_validator or _coverage_validator()
    canonical, path = _validated_repo_path(
        repo_root,
        path_text,
        label=label,
        coverage_validator=validator,
    )
    return {"path": canonical, "sha256": _stable_digest(path, label=label)}


def _load_validated_preflight_bytes(path: Path, preflight_validator: ModuleType) -> bytes:
    try:
        raw = preflight_validator._read_evidence_bytes(path)
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            parse_constant=preflight_validator._reject_constant,
            object_pairs_hook=preflight_validator._strict_object,
        )
        preflight_validator.validate_snapshot(value)
    except (
        preflight_validator.HostPreflightEvidenceError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
        OSError,
    ) as exc:
        raise HostProvenanceError("host preflight evidence is invalid") from exc
    return raw


def make_preflight_pin(
    repo_root: Path,
    path_text: str,
    *,
    coverage_validator: ModuleType | None = None,
    preflight_validator: ModuleType | None = None,
) -> dict[str, str]:
    coverage = coverage_validator or _coverage_validator()
    preflight = preflight_validator or _preflight_validator()
    canonical, path = _validated_repo_path(
        repo_root,
        path_text,
        label="host preflight path",
        coverage_validator=coverage,
    )
    raw = _load_validated_preflight_bytes(path, preflight)
    return {"path": canonical, "sha256": _sha256_bytes(raw)}


def _validate_pin(
    pin: object,
    *,
    repo_root: Path,
    label: str,
    coverage_validator: ModuleType,
) -> tuple[str, Path, str]:
    if not isinstance(pin, dict) or set(pin) != PIN_FIELDS:
        raise HostProvenanceError(f"{label} pin has an unexpected shape")
    if not _is_sha256(pin.get("sha256")):
        raise HostProvenanceError(f"{label} pin has an invalid digest")
    path_text, path = _validated_repo_path(
        repo_root,
        pin.get("path"),
        label=f"{label} path",
        coverage_validator=coverage_validator,
    )
    digest = _stable_digest(path, label=label)
    if digest != pin["sha256"]:
        raise HostProvenanceError(f"{label} digest does not match evidence")
    return path_text, path, digest


def validate_manifest(
    payload: dict[str, Any],
    *,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    if set(payload) != TOP_LEVEL_FIELDS:
        raise HostProvenanceError("host provenance manifest has an unexpected shape")
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        raise HostProvenanceError("unsupported host provenance schema_version")

    coverage = _coverage_validator()
    preflight = _preflight_validator()

    coverage_text, coverage_path, _coverage_digest = _validate_pin(
        payload["coverage"],
        repo_root=repo_root,
        label="coverage manifest",
        coverage_validator=coverage,
    )
    try:
        coverage_payload = coverage.load_manifest(coverage_path)
        coverage_summary = coverage.validate_manifest(coverage_payload, repo_root=repo_root)
    except (coverage.CapacityCoverageManifestError, OSError, UnicodeError) as exc:
        raise HostProvenanceError("coverage manifest is invalid") from exc
    if coverage_summary.get("schema_version") != 2 or coverage_summary.get("provenance_bound") is not True:
        raise HostProvenanceError("coverage manifest must use provenance-bound schema v2")

    load_plan_text, load_plan_path, _load_plan_digest = _validate_pin(
        payload["load_plan"],
        repo_root=repo_root,
        label="load plan",
        coverage_validator=coverage,
    )
    if load_plan_text != coverage_payload.get("load_plan"):
        raise HostProvenanceError("load plan pin does not match coverage manifest")

    coverage_entries = {
        entry["scenario_id"]: entry
        for entry in coverage_payload["reports"]
        if isinstance(entry, dict) and isinstance(entry.get("scenario_id"), str)
    }
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise HostProvenanceError("scenarios must be a non-empty list")

    seen: set[str] = set()
    pinned_paths: list[tuple[Path, str, str]] = [
        (coverage_path, payload["coverage"]["sha256"], "coverage manifest"),
        (load_plan_path, payload["load_plan"]["sha256"], "load plan"),
    ]
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != SCENARIO_FIELDS:
            raise HostProvenanceError("scenario provenance entry has an unexpected shape")
        scenario_id = scenario.get("scenario_id")
        if not isinstance(scenario_id, str) or not scenario_id or scenario_id != scenario_id.strip():
            raise HostProvenanceError("scenario_id is invalid")
        if scenario_id in seen:
            raise HostProvenanceError("duplicate scenario provenance entry")
        entry = coverage_entries.get(scenario_id)
        if entry is None:
            raise HostProvenanceError("scenario is not present in coverage manifest")
        seen.add(scenario_id)

        expected_paths = {
            "report": entry["path"],
            "trials": entry["trials_path"],
            "run_manifest": entry["run_manifest_path"],
        }
        for field, expected_path in expected_paths.items():
            path_text, path, _digest = _validate_pin(
                scenario[field],
                repo_root=repo_root,
                label=f"{field} evidence",
                coverage_validator=coverage,
            )
            if path_text != expected_path:
                raise HostProvenanceError(f"{field} pin does not match coverage manifest")
            pinned_paths.append((path, scenario[field]["sha256"], f"{field} evidence"))

        preflight_pin = scenario["host_preflight"]
        if not isinstance(preflight_pin, dict) or set(preflight_pin) != PIN_FIELDS:
            raise HostProvenanceError("host preflight pin has an unexpected shape")
        if not _is_sha256(preflight_pin.get("sha256")):
            raise HostProvenanceError("host preflight pin has an invalid digest")
        _preflight_text, preflight_path = _validated_repo_path(
            repo_root,
            preflight_pin.get("path"),
            label="host preflight path",
            coverage_validator=coverage,
        )
        raw_preflight = _load_validated_preflight_bytes(preflight_path, preflight)
        if _sha256_bytes(raw_preflight) != preflight_pin["sha256"]:
            raise HostProvenanceError("host preflight digest does not match evidence")
        pinned_paths.append((preflight_path, preflight_pin["sha256"], "host preflight evidence"))

    if seen != set(coverage_entries):
        raise HostProvenanceError("scenario provenance does not exactly cover coverage manifest")

    # Re-read every pin after all semantic validators have run. This detects a
    # replacement or in-place mutation that occurs while the closure is being
    # validated rather than silently accepting a mixture of generations.
    for path, expected_digest, label in pinned_paths:
        if _stable_digest(path, label=label) != expected_digest:
            raise HostProvenanceError(f"{label} changed during validation")

    return {
        "valid": True,
        "schema_version": 1,
        "host_provenance_bound": True,
        "coverage_manifest": coverage_text,
        "scenario_ids": coverage_summary["scenario_ids"],
        "scenario_count": len(coverage_summary["scenario_ids"]),
        "node_profile": coverage_summary["node_profile"],
        "software_revision": coverage_summary["software_revision"],
        "recommended_max_sessions": coverage_summary["recommended_max_sessions"],
    }


def validate_manifest_file(path: Path, *, repo_root: Path = ROOT) -> dict[str, Any]:
    return validate_manifest(load_manifest(path), repo_root=repo_root)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Node-capacity host-provenance JSON")
    parser.add_argument("--json", action="store_true", help="emit a deterministic JSON summary")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = validate_manifest_file(args.manifest)
    except (HostProvenanceError, OSError, UnicodeError) as exc:
        print(f"node capacity host provenance invalid: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(summary, sys.stdout, ensure_ascii=False, sort_keys=True, allow_nan=False)
        sys.stdout.write("\n")
    else:
        print(
            "node capacity host provenance valid: "
            f"schema=v{summary['schema_version']} scenarios={summary['scenario_count']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
