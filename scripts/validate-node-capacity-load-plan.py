#!/usr/bin/env python3
"""Validate a rendered Issue #13 Node-capacity load plan without executing it."""

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

MAX_PLAN_BYTES = 256 * 1024
RENDERER_PATH = Path(__file__).with_name("render-node-capacity-load-plan.py")


class PlanValidationError(ValueError):
    """Raised when a load-plan file is unsafe, malformed, or non-canonical."""


def _load_renderer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("node_capacity_load_plan_renderer", RENDERER_PATH)
    if spec is None or spec.loader is None:
        raise PlanValidationError("cannot load load-plan renderer")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise PlanValidationError("cannot load load-plan renderer") from exc
    return module


def _reject_constant(value: str) -> None:
    raise PlanValidationError(f"non-standard JSON numeric constant: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlanValidationError(f"duplicate JSON key: {key}")
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


def _read_plan_bytes(path: Path) -> bytes:
    """Read one bounded stable regular file without following a final symlink."""

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise PlanValidationError(f"cannot inspect plan: {exc}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise PlanValidationError("plan must be a regular file")
    if before.st_size > MAX_PLAN_BYTES:
        raise PlanValidationError(f"plan exceeds maximum size of {MAX_PLAN_BYTES} bytes")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PlanValidationError(f"cannot open plan: {exc}") from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise PlanValidationError("plan must be a regular file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise PlanValidationError("plan changed while opening")
        if opened.st_size > MAX_PLAN_BYTES:
            raise PlanValidationError(f"plan exceeds maximum size of {MAX_PLAN_BYTES} bytes")

        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_PLAN_BYTES + 1)
        if len(raw) > MAX_PLAN_BYTES:
            raise PlanValidationError(f"plan exceeds maximum size of {MAX_PLAN_BYTES} bytes")

        after_read = os.fstat(fd)
        try:
            after_path = os.lstat(path)
        except OSError as exc:
            raise PlanValidationError("plan changed while reading") from exc
        if not stat.S_ISREG(after_path.st_mode):
            raise PlanValidationError("plan changed while reading")
        if _identity(opened) != _identity(after_read) or _identity(opened) != _identity(after_path):
            raise PlanValidationError("plan changed while reading")
        return raw
    finally:
        os.close(fd)


def load_plan(path: Path) -> dict[str, Any]:
    try:
        raw = _read_plan_bytes(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlanValidationError("plan must be valid UTF-8") from exc
    try:
        value = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_strict_object)
    except json.JSONDecodeError as exc:
        raise PlanValidationError(f"invalid JSON: {exc}") from exc
    except RecursionError as exc:
        raise PlanValidationError("plan JSON nesting is too deep") from exc
    if not isinstance(value, dict):
        raise PlanValidationError("plan root must be an object")
    return value


def validate_plan(value: dict[str, Any]) -> dict[str, Any]:
    renderer = _load_renderer()
    profile_label = value.get("profile_label")
    session_counts = value.get("session_counts")
    if not isinstance(profile_label, str):
        raise PlanValidationError("profile_label must be a string")
    if not isinstance(session_counts, list):
        raise PlanValidationError("session_counts must be a list")
    for count in session_counts:
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise PlanValidationError("session_counts must contain positive integers")

    baseline = set(renderer.BASELINE_SESSION_COUNTS)
    extras = [count for count in session_counts if count not in baseline]
    try:
        expected = renderer.build_plan(profile_label, extras)
    except renderer.PlanError as exc:
        raise PlanValidationError(str(exc)) from exc
    if value != expected:
        raise PlanValidationError("plan does not match the canonical renderer contract")

    return {
        "valid": True,
        "schema_version": value["schema_version"],
        "profile_label": value["profile_label"],
        "session_counts": value["session_counts"],
        "scenario_ids": [scenario["id"] for scenario in value["scenarios"]],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a canonical Issue #13 Node-capacity load-plan manifest."
    )
    parser.add_argument("plan", type=Path, help="Path to the rendered load-plan JSON file.")
    parser.add_argument("--json", action="store_true", help="Emit a deterministic JSON summary.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = validate_plan(load_plan(args.plan))
    except (PlanValidationError, OSError) as exc:
        print(f"node capacity load-plan validation failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(summary, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(
            "node capacity load-plan valid: "
            f"profile={summary['profile_label']} "
            f"levels={','.join(str(value) for value in summary['session_counts'])} "
            f"scenarios={len(summary['scenario_ids'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
