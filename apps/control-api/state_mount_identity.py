"""Read-only mount identity verification for Control Plane readiness.

The authority reader proves that state files are internally valid, but that is
not enough to prove that the configured state directory is backed by the
operator-intended mount. This helper optionally pins readiness to an expected
Linux mount source and/or mount root using only ``/proc/self/mountinfo``.

The check is deliberately opt-in: with no expected identity configured it does
nothing and preserves the existing readiness contract.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

try:
    from mountinfo_identity import (
        MountInfoError,
        identity_matches,
        normalize_expected_identity,
        parse_mountinfo_entries,
        select_exact_mounts,
    )
except ModuleNotFoundError:
    # Local development commonly starts uvicorn from apps/control-api. Docker
    # packages the shared module at /app, while a source checkout keeps it at
    # repository root; support both without depending on the caller's cwd.
    _REPO_ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(_REPO_ROOT))
    from mountinfo_identity import (  # type: ignore[no-redef]
        MountInfoError,
        identity_matches,
        normalize_expected_identity,
        parse_mountinfo_entries,
        select_exact_mounts,
    )


_DEFAULT_MOUNTINFO = Path("/proc/self/mountinfo")


class StateMountIdentityError(RuntimeError):
    """Raised when an explicitly configured state mount cannot be trusted."""


def expected_mount_identity_from_env(
    source_name: str,
    root_name: str,
) -> tuple[str | None, str | None] | None:
    """Return one opt-in expected identity without exposing configured values."""
    source_present = source_name in os.environ
    root_present = root_name in os.environ
    if not source_present and not root_present:
        return None

    source = os.environ.get(source_name) if source_present else None
    root = os.environ.get(root_name) if root_present else None
    return _normalize_expected_identity(source, root)


def _normalize_expected_identity(
    source: str | None,
    root: str | None,
) -> tuple[str | None, str | None]:
    try:
        return normalize_expected_identity(source, root)
    except MountInfoError as exc:
        raise StateMountIdentityError("invalid expected mount identity") from exc


def check_state_mount_identity(
    path: Path,
    *,
    expected_source: str | None,
    expected_root: str | None,
    mountinfo_path: Path = _DEFAULT_MOUNTINFO,
) -> None:
    """Verify one configured state directory against its expected mount entry.

    No expectation means no check. Once opted in, the target must be an exact,
    unique mountpoint and every configured identity field must match. The
    function never creates, writes, mounts, unmounts, or repairs anything.
    """
    if expected_source is None and expected_root is None:
        return
    expected_source, expected_root = _normalize_expected_identity(
        expected_source, expected_root
    )

    try:
        target_stat = os.lstat(path)
    except OSError as exc:
        raise StateMountIdentityError("state mount target is unavailable") from exc
    if not stat.S_ISDIR(target_stat.st_mode) or stat.S_ISLNK(target_stat.st_mode):
        raise StateMountIdentityError("state mount target is unavailable")

    try:
        text = mountinfo_path.read_text(encoding="utf-8", errors="surrogateescape")
        entries = parse_mountinfo_entries(text)
    except (OSError, UnicodeError, MountInfoError) as exc:
        raise StateMountIdentityError("mount identity evidence is unavailable") from exc

    matches = select_exact_mounts(entries, path)
    if not matches:
        raise StateMountIdentityError("expected state mountpoint is missing")
    if len(matches) != 1:
        raise StateMountIdentityError("state mountpoint identity is ambiguous")

    try:
        matches_identity = identity_matches(
            matches[0],
            expected_source=expected_source,
            expected_root=expected_root,
        )
    except MountInfoError as exc:
        raise StateMountIdentityError("mount identity evidence is unavailable") from exc

    if not matches_identity:
        raise StateMountIdentityError("state mount identity mismatch")
