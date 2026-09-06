"""Bounded garbage collection for expired authentication sessions.

This module is intentionally separate from request authentication. A GC run
uses the same process/file lock and atomic writer as ``auth_store`` so it cannot
race login/logout writers or silently recreate an authority file that was
previously initialized and then lost.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from auth_store import (
    AUTH_SESSIONS_PATH,
    _default_sessions,
    _state_lock,
    _validate_sessions,
    atomic_write_json,
    read_json,
)


DEFAULT_MAX_DELETIONS = 1_000
MAX_DELETIONS_PER_RUN = 10_000


@dataclass(frozen=True)
class PruneResult:
    scanned: int
    deleted: int
    expired_remaining: int
    dry_run: bool

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "scanned": self.scanned,
            "deleted": self.deleted,
            "expired_remaining": self.expired_remaining,
            "dry_run": self.dry_run,
        }


def _validated_now(value: float | None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("now must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("now must be a finite number")
    return number


def _validated_max_deletions(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max deletions must be an integer")
    if value <= 0 or value > MAX_DELETIONS_PER_RUN:
        raise ValueError(
            f"max deletions must be between 1 and {MAX_DELETIONS_PER_RUN}"
        )
    return value


def _expired_token_hashes(
    state: dict[str, Any], *, now: float, max_deletions: int
) -> tuple[list[str], int]:
    """Return an oldest-first bounded deletion set and total expired count."""
    validated = _validate_sessions(state)
    expired = [
        (float(record["expires_at"]), token_hash)
        for token_hash, record in validated["sessions"].items()
        if float(record["expires_at"]) <= now
    ]
    expired.sort(key=lambda item: (item[0], item[1]))
    selected = [token_hash for _, token_hash in expired[:max_deletions]]
    return selected, len(expired)


def prune_expired_sessions(
    *,
    now: float | None = None,
    max_deletions: int = DEFAULT_MAX_DELETIONS,
    dry_run: bool = False,
    path: Path = AUTH_SESSIONS_PATH,
) -> PruneResult:
    """Remove at most ``max_deletions`` expired auth sessions atomically.

    The whole authority is validated before any deletion. Corrupt state fails
    closed and is never rewritten. A run that has nothing to delete also avoids
    an unnecessary authority write.
    """
    effective_now = _validated_now(now)
    limit = _validated_max_deletions(max_deletions)

    with _state_lock(exclusive=True):
        state = _validate_sessions(read_json(path, _default_sessions()))
        scanned = len(state["sessions"])
        selected, expired_count = _expired_token_hashes(
            state, now=effective_now, max_deletions=limit
        )
        if selected and not dry_run:
            for token_hash in selected:
                del state["sessions"][token_hash]
            atomic_write_json(path, state)

    deleted = 0 if dry_run else len(selected)
    remaining = expired_count if dry_run else expired_count - len(selected)
    return PruneResult(
        scanned=scanned,
        deleted=deleted,
        expired_remaining=remaining,
        dry_run=dry_run,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prune expired IRLight authentication sessions safely"
    )
    parser.add_argument(
        "--max-delete",
        type=int,
        default=DEFAULT_MAX_DELETIONS,
        help=f"maximum expired records to delete (1-{MAX_DELETIONS_PER_RUN})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report counts without rewriting auth_sessions.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = prune_expired_sessions(
        max_deletions=args.max_delete,
        dry_run=args.dry_run,
    )
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
