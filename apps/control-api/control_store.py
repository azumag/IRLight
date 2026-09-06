"""Durable, process-safe authority for the legacy audio control command."""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from state_safety import load_json_authority, mark_initialized, was_initialized


_PROCESS_LOCK = threading.RLock()


class ControlStateError(RuntimeError):
    pass


class ControlIdempotencyConflict(ControlStateError):
    pass


class ControlVersionConflict(ControlStateError):
    def __init__(self, current: dict[str, object]) -> None:
        super().__init__("control version conflict")
        self.current = current


def default_control(*, now: float | None = None) -> dict[str, object]:
    return {
        "audio_mode": "LIVE",
        "version": 0,
        "command_id": None,
        "idempotency_key": None,
        "updated_at": time.time() if now is None else now,
    }


def _validate_control(value: Any) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ControlStateError("control state has invalid structure")
    mode = value.get("audio_mode")
    version = value.get("version")
    command_id = value.get("command_id")
    idempotency_key = value.get("idempotency_key")
    updated_at = value.get("updated_at")
    if mode not in {"LIVE", "MUTED"}:
        raise ControlStateError("control state has invalid audio mode")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ControlStateError("control state has invalid version")
    if command_id is not None:
        if not isinstance(command_id, str):
            raise ControlStateError("control state has invalid command id")
        try:
            uuid.UUID(command_id)
        except ValueError as exc:
            raise ControlStateError("control state has invalid command id") from exc
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or len(idempotency_key) > 200
    ):
        raise ControlStateError("control state has invalid idempotency key")
    if (
        isinstance(updated_at, bool)
        or not isinstance(updated_at, (int, float))
        or not math.isfinite(float(updated_at))
    ):
        raise ControlStateError("control state has invalid update time")
    return {
        "audio_mode": mode,
        "version": version,
        "command_id": command_id,
        "idempotency_key": idempotency_key,
        "updated_at": float(updated_at),
    }


class ControlStore:
    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "control.json"
        self.lock_path = self.state_dir / ".control-state.lock"

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        with _PROCESS_LOCK:
            try:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                with self.lock_path.open("a+", encoding="utf-8") as handle:
                    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                    fcntl.flock(handle.fileno(), operation)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ControlStateError:
                raise
            except OSError as exc:
                raise ControlStateError("control state cannot be locked") from exc

    def _read(self) -> dict[str, object]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = load_json_authority(handle)
        except FileNotFoundError:
            if was_initialized(self.path):
                raise ControlStateError(
                    "control state disappeared after initialization"
                )
            raise ControlStateError("control state is not initialized")
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            raise ControlStateError("control state cannot be read") from exc
        return _validate_control(value)

    def _write(self, value: dict[str, object]) -> None:
        validated = _validate_control(value)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.state_dir
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(validated, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            # Arm the durable fuse before publishing the first authoritative
            # state. A crash after this point may fail closed, but can never
            # make a previously attempted authority look uninitialized.
            mark_initialized(self.path)
            os.replace(temporary, self.path)
            directory_fd = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def ensure(self) -> None:
        with self._lock(exclusive=True):
            if self.path.exists():
                self._read()
                mark_initialized(self.path)
                return
            if was_initialized(self.path):
                raise ControlStateError(
                    "control state disappeared after initialization"
                )
            self._write(default_control())

    def get(self) -> dict[str, object]:
        with self._lock(exclusive=False):
            return self._read()

    def update(
        self,
        *,
        mode: str,
        idempotency_key: str,
        expected_version: int | None = None,
        now: float | None = None,
    ) -> dict[str, object]:
        if mode not in {"LIVE", "MUTED"}:
            raise ValueError("unsupported audio mode")
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("invalid idempotency key")
        with self._lock(exclusive=True):
            current = self._read()
            current_version = int(current["version"])
            if current.get("idempotency_key") == idempotency_key:
                if current.get("audio_mode") != mode:
                    raise ControlIdempotencyConflict(
                        "idempotency key was already used for another mode"
                    )
                return current
            if expected_version is not None and expected_version != current_version:
                raise ControlVersionConflict(current)
            next_control: dict[str, object] = {
                "audio_mode": mode,
                "version": current_version + 1,
                "command_id": str(uuid.uuid4()),
                "idempotency_key": idempotency_key,
                "updated_at": time.time() if now is None else now,
            }
            self._write(next_control)
            return next_control
