"""Read-only concurrent Session capacity inspection for operators.

This inspector mirrors the capacity accounting used by ``SessionStore`` without
instantiating stores or acquiring their locks. It reads the persisted Session
and entitlement authority directly and emits only aggregate counts. It never
creates state directories, lock files, initialization markers, Sessions, or
provider resources.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import Any

from entitlement_store import EntitlementStateError, EntitlementStore, _default_limit
from session_store import CAPACITY_STATES, SessionStateError, SessionStore
from state_safety import load_json_authority, was_initialized


class SessionCapacityInspectError(RuntimeError):
    """Raised when capacity authority cannot be inspected safely."""


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constants are not allowed")


def _read_optional_json_authority(
    path: Path, *, default: dict[str, Any]
) -> dict[str, Any]:
    """Read one authority file without creating any filesystem entry."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        try:
            initialized = was_initialized(path)
        except OSError as exc:
            raise SessionCapacityInspectError(
                "required state cannot be inspected"
            ) from exc
        if initialized:
            raise SessionCapacityInspectError(
                "required state disappeared after initialization"
            )
        return default
    except OSError as exc:
        raise SessionCapacityInspectError("required state cannot be inspected") from exc

    if not stat.S_ISREG(before.st_mode):
        raise SessionCapacityInspectError("required state is not a regular file")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SessionCapacityInspectError("required state cannot be opened") from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise SessionCapacityInspectError("required state is not a regular file")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise SessionCapacityInspectError(
                "required state changed during inspection"
            )

        try:
            handle = os.fdopen(fd, "r", encoding="utf-8")
        except OSError as exc:
            raise SessionCapacityInspectError("required state cannot be read") from exc
        fd = -1
        with handle:
            try:
                value = load_json_authority(
                    handle, parse_constant=_reject_json_constant
                )
            except (
                json.JSONDecodeError,
                UnicodeDecodeError,
                ValueError,
                OSError,
            ) as exc:
                raise SessionCapacityInspectError(
                    "required state contains invalid JSON"
                ) from exc
    finally:
        if fd >= 0:
            os.close(fd)

    if not isinstance(value, dict):
        raise SessionCapacityInspectError("required state has invalid structure")
    return value


def _read_sessions(state_dir: Path) -> dict[str, dict[str, Any]]:
    raw = _read_optional_json_authority(
        state_dir / "sessions.json",
        default={"sessions": {}, "orphan_cleanup_leases": {}},
    )
    sessions = raw.get("sessions")
    leases = raw.get("orphan_cleanup_leases", {})
    if not isinstance(sessions, dict) or not isinstance(leases, dict):
        raise SessionCapacityInspectError("Session authority has invalid structure")
    try:
        SessionStore._validate_sessions(sessions)
        SessionStore._validate_cleanup_leases(leases)
    except SessionStateError as exc:
        raise SessionCapacityInspectError(
            "Session authority failed validation"
        ) from exc
    return sessions


def _read_entitlements(state_dir: Path) -> dict[str, dict[str, Any]]:
    raw = _read_optional_json_authority(
        state_dir / "entitlements.json",
        default={"entitlements": {}},
    )
    entitlements = raw.get("entitlements")
    if not isinstance(entitlements, dict):
        raise SessionCapacityInspectError("entitlement authority has invalid structure")
    try:
        EntitlementStore._validate_entitlements(entitlements)
    except EntitlementStateError as exc:
        raise SessionCapacityInspectError(
            "entitlement authority failed validation"
        ) from exc
    return entitlements


def summarize_session_capacity(
    sessions: dict[str, dict[str, Any]],
    entitlements: dict[str, dict[str, Any]],
    *,
    user_id: str,
    default_limit: int,
) -> dict[str, Any]:
    """Mirror SessionStore's per-user concurrent-capacity accounting."""
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id must not be empty")
    if (
        isinstance(default_limit, bool)
        or not isinstance(default_limit, int)
        or default_limit < 0
    ):
        raise ValueError("default_limit must be a non-negative integer")

    entitlement = entitlements.get(user_id)
    limit = (
        int(entitlement["max_concurrent_sessions"])
        if entitlement is not None
        else default_limit
    )

    occupied = sum(
        1
        for session in sessions.values()
        if session.get("user_id") == user_id
        and (
            session.get("entitlement_reserved") is True
            or session.get("status") in CAPACITY_STATES
        )
    )
    available = max(0, limit - occupied)

    if limit == 0:
        status = "CAPACITY_DISABLED"
    elif occupied >= limit:
        status = "CAPACITY_EXHAUSTED"
    else:
        status = "CAPACITY_AVAILABLE"

    return {
        "status": status,
        "limit": limit,
        "occupied": occupied,
        "available": available,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="session_capacity_inspect_cli")
    parser.add_argument(
        "--user-id",
        required=True,
        help="User whose concurrent Session capacity is inspected",
    )
    parser.add_argument(
        "--state-dir",
        default=os.getenv("STATE_DIR", "/state"),
        help="Control Plane state directory (default: STATE_DIR or /state)",
    )
    args = parser.parse_args(argv)

    try:
        state_dir = Path(args.state_dir)
        sessions = _read_sessions(state_dir)
        entitlements = _read_entitlements(state_dir)
        payload = summarize_session_capacity(
            sessions,
            entitlements,
            user_id=args.user_id,
            default_limit=_default_limit(),
        )
    except SessionCapacityInspectError:
        print(
            json.dumps(
                {
                    "status": "CAPACITY_UNAVAILABLE",
                    "reason": "capacity authority unavailable",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 3

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 2 if payload["status"] != "CAPACITY_AVAILABLE" else 0


if __name__ == "__main__":
    raise SystemExit(main())
