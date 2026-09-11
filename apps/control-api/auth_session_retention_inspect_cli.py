"""Read-only retention inspection for authentication session authority.

The inspector intentionally does not acquire the auth-store lock because that
lock path can create filesystem entries. Auth writers replace the JSON file
atomically, so opening one regular-file inode gives this command a consistent
byte snapshot without mutating authority state.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from state_safety import load_json_authority


DEFAULT_STATE_FILE = Path(os.getenv("STATE_DIR", "/state")) / "auth_sessions.json"
DEFAULT_MAX_DELETIONS = 1_000
MAX_DELETIONS_PER_RUN = 10_000
TOKEN_HASH_LENGTH = 64
CSRF_TOKEN_BYTES = 24
CSRF_TOKEN_LENGTH = (CSRF_TOKEN_BYTES * 4 + 2) // 3
TOKEN_URLSAFE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


class RetentionInspectError(ValueError):
    """Raised when auth-session authority cannot be inspected safely."""


@dataclass(frozen=True)
class RetentionSnapshot:
    sessions_total: int
    sessions_active: int
    sessions_expired: int
    gc_runs_required: int
    users_with_active_sessions: int
    users_with_multiple_active_sessions: int
    max_active_sessions_per_user: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "status": "OK",
            "sessions_total": self.sessions_total,
            "sessions_active": self.sessions_active,
            "sessions_expired": self.sessions_expired,
            "gc_runs_required": self.gc_runs_required,
            "users_with_active_sessions": self.users_with_active_sessions,
            "users_with_multiple_active_sessions": self.users_with_multiple_active_sessions,
            "max_active_sessions_per_user": self.max_active_sessions_per_user,
        }


def _finite_number(record: dict[str, Any], field: str) -> float:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RetentionInspectError("authentication session authority is invalid")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise RetentionInspectError("authentication session authority is invalid") from None
    if not math.isfinite(number):
        raise RetentionInspectError("authentication session authority is invalid")
    return number


def _validate_record(token_hash: Any, record: Any) -> tuple[float, str]:
    if (
        not isinstance(token_hash, str)
        or len(token_hash) != TOKEN_HASH_LENGTH
        or any(char not in "0123456789abcdef" for char in token_hash)
        or not isinstance(record, dict)
    ):
        raise RetentionInspectError("authentication session authority is invalid")

    user_id = record.get("user_id")
    csrf_token = record.get("csrf_token")
    if not isinstance(user_id, str) or not user_id:
        raise RetentionInspectError("authentication session authority is invalid")
    if (
        not isinstance(csrf_token, str)
        or len(csrf_token) != CSRF_TOKEN_LENGTH
        or any(char not in TOKEN_URLSAFE_CHARS for char in csrf_token)
    ):
        raise RetentionInspectError("authentication session authority is invalid")

    _finite_number(record, "created_at")
    return _finite_number(record, "expires_at"), user_id


def _validated_now(value: float | None) -> float:
    candidate = time.time() if value is None else value
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise RetentionInspectError("inspection time is invalid")
    try:
        number = float(candidate)
    except (OverflowError, ValueError):
        raise RetentionInspectError("inspection time is invalid") from None
    if not math.isfinite(number):
        raise RetentionInspectError("inspection time is invalid")
    return number


def _validated_max_deletions(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RetentionInspectError("GC deletion limit is invalid")
    if value <= 0 or value > MAX_DELETIONS_PER_RUN:
        raise RetentionInspectError("GC deletion limit is invalid")
    return value


def _load_read_only_snapshot(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        before = path.lstat()
        fd = os.open(path, flags)
    except (OSError, ValueError) as exc:
        raise RetentionInspectError("authentication session authority is unavailable") from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(opened.st_mode):
            raise RetentionInspectError("authentication session authority is unavailable")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise RetentionInspectError("authentication session authority changed while opening")
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                value = load_json_authority(handle)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RetentionInspectError("authentication session authority is invalid") from exc
    finally:
        if fd >= 0:
            os.close(fd)

    if not isinstance(value, dict) or not isinstance(value.get("sessions"), dict):
        raise RetentionInspectError("authentication session authority is invalid")
    return value


def inspect_auth_session_retention(
    *,
    path: Path = DEFAULT_STATE_FILE,
    now: float | None = None,
    max_deletions: int = DEFAULT_MAX_DELETIONS,
) -> RetentionSnapshot:
    """Return aggregate retention counts without creating or rewriting state."""

    effective_now = _validated_now(now)
    limit = _validated_max_deletions(max_deletions)
    state = _load_read_only_snapshot(path)

    total = 0
    expired = 0
    active_sessions_by_user: dict[str, int] = {}
    for token_hash, record in state["sessions"].items():
        expires_at, user_id = _validate_record(token_hash, record)
        total += 1
        if expires_at <= effective_now:
            expired += 1
            continue
        active_sessions_by_user[user_id] = active_sessions_by_user.get(user_id, 0) + 1

    return RetentionSnapshot(
        sessions_total=total,
        sessions_active=total - expired,
        sessions_expired=expired,
        gc_runs_required=(expired + limit - 1) // limit,
        users_with_active_sessions=len(active_sessions_by_user),
        users_with_multiple_active_sessions=sum(
            count > 1 for count in active_sessions_by_user.values()
        ),
        max_active_sessions_per_user=max(active_sessions_by_user.values(), default=0),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect IRLight authentication-session retention without mutation"
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="auth_sessions.json to inspect (default: STATE_DIR/auth_sessions.json)",
    )
    parser.add_argument(
        "--max-delete",
        type=int,
        default=DEFAULT_MAX_DELETIONS,
        help=f"GC batch size used only to estimate runs (1-{MAX_DELETIONS_PER_RUN})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        snapshot = inspect_auth_session_retention(
            path=args.state_file,
            max_deletions=args.max_delete,
        )
    except RetentionInspectError:
        print(
            json.dumps(
                {
                    "status": "UNAVAILABLE",
                    "reason_code": "AUTH_SESSION_STATE_UNAVAILABLE",
                },
                sort_keys=True,
            )
        )
        return 2

    print(json.dumps(snapshot.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
