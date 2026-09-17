#!/usr/bin/env python3
"""Assemble strict IRLight Node-capacity trial JSONL into a schema-v1 report."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


class CapacityAssemblyError(ValueError):
    """Raised when raw capacity evidence cannot be safely assembled."""


def _reject_constant(value: str) -> None:
    raise CapacityAssemblyError(f"non-standard JSON numeric constant: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapacityAssemblyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _open_trials_readonly(path: Path) -> Any:
    """Open one stable regular-file snapshot without following a symlink."""

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise CapacityAssemblyError(f"cannot inspect trials: {exc}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise CapacityAssemblyError("trials JSONL must be a regular file")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CapacityAssemblyError(f"cannot open trials: {exc}") from exc
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode):
            raise CapacityAssemblyError("trials JSONL must be a regular file")
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise CapacityAssemblyError("trials JSONL changed while opening")
        return os.fdopen(fd, "r", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def load_trials_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        with _open_trials_readonly(path) as handle:
            raw = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise CapacityAssemblyError(f"cannot read trials: {exc}") from exc

    lines = raw.splitlines()
    if not lines:
        raise CapacityAssemblyError("trials JSONL must contain at least one trial")

    trials: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise CapacityAssemblyError(
                f"trials JSONL line {line_number} must not be blank"
            )
        try:
            value = json.loads(
                line,
                parse_constant=_reject_constant,
                object_pairs_hook=_strict_object,
            )
        except json.JSONDecodeError as exc:
            raise CapacityAssemblyError(
                f"invalid JSON on trials JSONL line {line_number}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise CapacityAssemblyError(
                f"trials JSONL line {line_number} must contain a JSON object"
            )
        trials.append(value)
    return trials


def _open_trials_lock(path: Path) -> Any:
    lock_path = path.with_name(f"{path.name}.lock")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise CapacityAssemblyError(f"cannot open trial lock: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CapacityAssemblyError("trial lock must be a regular file")
        return os.fdopen(fd, "r+", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def load_trials_snapshot(path: Path) -> list[dict[str, Any]]:
    """Read a complete trial snapshot while cooperating recorders are excluded."""

    if path.parent and not path.parent.exists():
        raise CapacityAssemblyError("trials JSONL parent directory does not exist")
    with _open_trials_lock(path) as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH)
        return load_trials_jsonl(path)


def _load_validator() -> Any:
    path = Path(__file__).with_name("validate-node-capacity-report.py")
    spec = importlib.util.spec_from_file_location(
        "irlight_validate_node_capacity_report", path
    )
    if spec is None or spec.loader is None:
        raise CapacityAssemblyError("cannot load Node capacity report validator")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError) as exc:
        raise CapacityAssemblyError(
            f"cannot load Node capacity report validator: {exc}"
        ) from exc
    return module


def assemble_report(
    *,
    trials: list[dict[str, Any]],
    run_id: str,
    node_profile: str,
    software_revision: str,
    scenario: str,
    safety_margin_percent: int,
    notes: str,
) -> dict[str, Any]:
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "node_profile": node_profile,
        "software_revision": software_revision,
        "scenario": scenario,
        "safety_margin_percent": safety_margin_percent,
        "trials": trials,
        "notes": notes,
    }
    validator = _load_validator()
    try:
        validator.validate_report(report)
    except (ValueError, TypeError, OverflowError) as exc:
        raise CapacityAssemblyError(f"assembled report is invalid: {exc}") from exc
    return report


def _write_exclusive_atomic(path: Path, rendered: str) -> None:
    """Publish a complete report without exposing a partially written final path."""

    parent = path.parent
    if parent and not parent.exists():
        raise CapacityAssemblyError("output parent directory does not exist")
    directory = parent if parent else Path(".")
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.tmp-",
            dir=directory,
        )
    except OSError as exc:
        raise CapacityAssemblyError(f"cannot create temporary output: {exc}") from exc

    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, UnicodeEncodeError) as exc:
            raise CapacityAssemblyError(f"cannot write temporary output: {exc}") from exc
        finally:
            if fd >= 0:
                os.close(fd)

        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise CapacityAssemblyError("output already exists") from exc
        except OSError as exc:
            raise CapacityAssemblyError(f"cannot publish output atomically: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials-jsonl", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--node-profile", required=True)
    parser.add_argument("--software-revision", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--safety-margin-percent", type=int, required=True)
    parser.add_argument("--notes", default="")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.output is not None:
            try:
                if args.output.resolve() == args.trials_jsonl.resolve():
                    raise CapacityAssemblyError(
                        "output must not overwrite the raw trials JSONL"
                    )
            except OSError as exc:
                raise CapacityAssemblyError(f"cannot resolve paths: {exc}") from exc

        trials = load_trials_snapshot(args.trials_jsonl)
        report = assemble_report(
            trials=trials,
            run_id=args.run_id,
            node_profile=args.node_profile,
            software_revision=args.software_revision,
            scenario=args.scenario,
            safety_margin_percent=args.safety_margin_percent,
            notes=args.notes,
        )
        rendered = (
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            _write_exclusive_atomic(args.output, rendered)
    except (CapacityAssemblyError, OSError, UnicodeEncodeError) as exc:
        print(f"node capacity report assembly failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
