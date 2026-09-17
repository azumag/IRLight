#!/usr/bin/env python3
"""Append one measured Node-capacity trial to a strict raw JSONL evidence file."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


class CapacityTrialRecordError(ValueError):
    """Raised when a raw capacity trial cannot be safely recorded."""


def _load_script(filename: str, module_name: str) -> Any:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise CapacityTrialRecordError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError) as exc:
        raise CapacityTrialRecordError(f"cannot load {filename}: {exc}") from exc
    return module


def build_trial(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "concurrent_sessions": args.concurrent_sessions,
        "duration_seconds": args.duration_seconds,
        "outcome": args.outcome,
        "cpu_peak_percent": args.cpu_peak_percent,
        "memory_rss_peak_bytes": args.memory_rss_peak_bytes,
        "egress_peak_bps": args.egress_peak_bps,
        "failed_sessions": args.failed_sessions,
        "unexpected_reconnects": args.unexpected_reconnects,
    }


def _open_lock(path: Path) -> Any:
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CapacityTrialRecordError(f"cannot open trial lock: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CapacityTrialRecordError("trial lock must be a regular file")
        return os.fdopen(fd, "r+", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def _open_trials_for_update(path: Path) -> tuple[int, bool]:
    """Open or create the evidence file once and pin the validated inode."""

    flags = os.O_RDWR | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)

    try:
        before = os.lstat(path)
    except FileNotFoundError:
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise CapacityTrialRecordError(
                "trials JSONL appeared while opening"
            ) from exc
        except OSError as exc:
            raise CapacityTrialRecordError(
                f"cannot create trials JSONL: {exc}"
            ) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise CapacityTrialRecordError("trials JSONL must be a regular file")
            return fd, True
        except Exception:
            os.close(fd)
            raise
    except OSError as exc:
        raise CapacityTrialRecordError(f"cannot inspect trials JSONL: {exc}") from exc

    if stat.S_ISLNK(before.st_mode):
        raise CapacityTrialRecordError("trials JSONL must not be a symbolic link")
    if not stat.S_ISREG(before.st_mode):
        raise CapacityTrialRecordError("trials JSONL must be a regular file")
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CapacityTrialRecordError(f"cannot open trials JSONL: {exc}") from exc
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode):
            raise CapacityTrialRecordError("trials JSONL must be a regular file")
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise CapacityTrialRecordError("trials JSONL changed while opening")
        return fd, False
    except Exception:
        os.close(fd)
        raise


def _verify_trials_path(
    fd: int,
    path: Path,
    *,
    expected_size: int | None = None,
) -> os.stat_result:
    """Ensure the public path still names the inode pinned by fd."""

    try:
        current = os.lstat(path)
        opened = os.fstat(fd)
    except OSError as exc:
        raise CapacityTrialRecordError(
            f"cannot verify trials JSONL identity: {exc}"
        ) from exc
    if not stat.S_ISREG(current.st_mode) or not stat.S_ISREG(opened.st_mode):
        raise CapacityTrialRecordError("trials JSONL must be a regular file")
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise CapacityTrialRecordError("trials JSONL changed while recording")
    if expected_size is not None and opened.st_size != expected_size:
        raise CapacityTrialRecordError("trials JSONL changed while recording")
    return opened


def _read_trials_fd(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CapacityTrialRecordError(f"cannot read trials JSONL: {exc}") from exc


def _needs_line_separator_fd(fd: int, info: os.stat_result) -> bool:
    try:
        current = os.fstat(fd)
    except OSError as exc:
        raise CapacityTrialRecordError(
            f"cannot inspect trials JSONL ending: {exc}"
        ) from exc
    if current.st_size != info.st_size:
        raise CapacityTrialRecordError("trials JSONL changed while recording")
    if info.st_size == 0:
        return False
    try:
        os.lseek(fd, -1, os.SEEK_END)
        return os.read(fd, 1) != b"\n"
    except OSError as exc:
        raise CapacityTrialRecordError(
            f"cannot inspect trials JSONL ending: {exc}"
        ) from exc


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        try:
            written = os.write(fd, view)
        except OSError as exc:
            raise CapacityTrialRecordError(
                f"cannot append trials JSONL: {exc}"
            ) from exc
        if written <= 0:
            raise CapacityTrialRecordError("cannot append trials JSONL: short write")
        view = view[written:]


def _rollback_append(fd: int, original_size: int) -> None:
    """Best-effort runtime rollback for a failed append on the pinned inode."""

    try:
        os.ftruncate(fd, original_size)
        os.fsync(fd)
        restored = os.fstat(fd)
    except OSError as exc:
        raise CapacityTrialRecordError(
            f"cannot roll back failed trials JSONL append: {exc}"
        ) from exc
    if restored.st_size != original_size:
        raise CapacityTrialRecordError(
            "cannot roll back failed trials JSONL append: size mismatch"
        )


def _remove_empty_created_trials_file(fd: int, path: Path) -> None:
    """Remove a newly-created evidence file only when it is still our empty inode."""

    try:
        opened = os.fstat(fd)
        current = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CapacityTrialRecordError(
            f"cannot inspect failed new trials JSONL: {exc}"
        ) from exc

    if opened.st_size != 0:
        return
    if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
        return
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CapacityTrialRecordError(
            f"cannot remove failed new trials JSONL: {exc}"
        ) from exc


def _append_line_fd(
    fd: int,
    path: Path,
    line: str,
    *,
    prepend_newline: bool = False,
    expected_size: int | None = None,
) -> None:
    """Append through the same inode that was parsed and validated."""

    before = _verify_trials_path(fd, path, expected_size=expected_size)
    payload = (("\n" if prepend_newline else "") + line).encode("utf-8")
    try:
        _write_all(fd, payload)
        try:
            os.fsync(fd)
        except OSError as exc:
            raise CapacityTrialRecordError(f"cannot sync trials JSONL: {exc}") from exc
        _verify_trials_path(fd, path, expected_size=before.st_size + len(payload))
    except Exception as exc:
        try:
            _rollback_append(fd, before.st_size)
        except CapacityTrialRecordError as rollback_exc:
            raise CapacityTrialRecordError(
                f"trials JSONL append failed and rollback failed: {rollback_exc}"
            ) from exc
        raise


def append_trial(path: Path, trial: dict[str, Any]) -> dict[str, Any]:
    if path.parent and not path.parent.exists():
        raise CapacityTrialRecordError("trials JSONL parent directory does not exist")

    assembler = _load_script(
        "assemble-node-capacity-report.py",
        "irlight_assemble_node_capacity_report_for_recorder",
    )
    validator = _load_script(
        "validate-node-capacity-report.py",
        "irlight_validate_node_capacity_report_for_recorder",
    )
    try:
        recorded = validator.normalize_trials([trial], minimum_count=1)[0]
    except (ValueError, TypeError, OverflowError) as exc:
        raise CapacityTrialRecordError(f"trial is invalid: {exc}") from exc

    lock_path = path.with_name(f"{path.name}.lock")
    with _open_lock(lock_path) as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        fd, created = _open_trials_for_update(path)
        try:
            try:
                info = _verify_trials_path(fd, path)
                raw = _read_trials_fd(fd)
                existing = [] if created else assembler.parse_trials_jsonl(raw)
                try:
                    normalized = validator.normalize_trials(
                        existing + [recorded], minimum_count=1
                    )
                except (ValueError, TypeError, OverflowError) as exc:
                    raise CapacityTrialRecordError(
                        f"trial sequence is invalid: {exc}"
                    ) from exc

                recorded = normalized[-1]
                line = (
                    json.dumps(
                        recorded,
                        ensure_ascii=False,
                        sort_keys=True,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                prepend_newline = _needs_line_separator_fd(fd, info)
                _append_line_fd(
                    fd,
                    path,
                    line,
                    prepend_newline=prepend_newline,
                    expected_size=info.st_size,
                )
                return recorded
            except assembler.CapacityAssemblyError as exc:
                raise CapacityTrialRecordError(
                    f"trial sequence is invalid: {exc}"
                ) from exc
        except Exception as exc:
            if created:
                try:
                    _remove_empty_created_trials_file(fd, path)
                except CapacityTrialRecordError as cleanup_exc:
                    raise CapacityTrialRecordError(
                        f"trial recording failed and cleanup failed: {cleanup_exc}"
                    ) from exc
            raise
        finally:
            os.close(fd)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials-jsonl", type=Path, required=True)
    parser.add_argument("--concurrent-sessions", type=int, required=True)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--outcome", choices=("pass", "fail"), required=True)
    parser.add_argument("--cpu-peak-percent", type=float, required=True)
    parser.add_argument("--memory-rss-peak-bytes", type=int, required=True)
    parser.add_argument("--egress-peak-bps", type=float, required=True)
    parser.add_argument("--failed-sessions", type=int, required=True)
    parser.add_argument("--unexpected-reconnects", type=int, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        recorded = append_trial(args.trials_jsonl, build_trial(args))
    except (CapacityTrialRecordError, OSError, UnicodeError) as exc:
        print(f"node capacity trial record failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            recorded,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
