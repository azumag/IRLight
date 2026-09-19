"""Read-only mount identity verification for Control Plane readiness.

The authority reader proves that state files are internally valid, but that is
not enough to prove that the configured state directory is backed by the
operator-intended mount.  This helper optionally pins readiness to an expected
Linux mount source and/or mount root using only ``/proc/self/mountinfo``.

The check is deliberately opt-in: with no expected identity configured it does
nothing and preserves the existing readiness contract.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path


_DEFAULT_MOUNTINFO = Path("/proc/self/mountinfo")
_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
_ALLOWED_ESCAPES = {"011", "012", "040", "134"}


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
    if source is not None and (not source or "\x00" in source):
        raise StateMountIdentityError("invalid expected mount identity")

    normalized_root: str | None = None
    if root is not None:
        if not root or "\x00" in root:
            raise StateMountIdentityError("invalid expected mount identity")
        root_path = Path(root)
        if not root_path.is_absolute():
            raise StateMountIdentityError("invalid expected mount identity")
        normalized_root = os.path.normpath(str(root_path))

    if source is None and normalized_root is None:
        raise StateMountIdentityError("invalid expected mount identity")
    return source, normalized_root


def _decode_mountinfo_field(raw: str) -> str:
    cursor = 0
    decoded: list[str] = []
    for match in _ESCAPE_RE.finditer(raw):
        literal = raw[cursor : match.start()]
        if "\\" in literal or match.group(1) not in _ALLOWED_ESCAPES:
            raise ValueError("invalid mountinfo escape")
        decoded.append(literal)
        decoded.append(chr(int(match.group(1), 8)))
        cursor = match.end()
    tail = raw[cursor:]
    if "\\" in tail:
        raise ValueError("invalid mountinfo escape")
    decoded.append(tail)
    return "".join(decoded)


def _mount_entries(text: str) -> list[tuple[str, str, str]]:
    if not text:
        raise ValueError("empty mountinfo")

    entries: list[tuple[str, str, str]] = []
    for raw_line in text.splitlines():
        if not raw_line:
            raise ValueError("empty mountinfo record")
        fields = raw_line.split()
        if len(fields) < 10:
            raise ValueError("short mountinfo record")
        try:
            separator = fields.index("-", 6)
        except ValueError as exc:
            raise ValueError("missing mountinfo separator") from exc
        if separator + 3 >= len(fields):
            raise ValueError("short mountinfo suffix")

        mountpoint = _decode_mountinfo_field(fields[4])
        if not mountpoint.startswith("/"):
            raise ValueError("relative mountpoint")
        # Keep root/source opaque until an exact target match is selected.  An
        # unrelated mount with an unusual source must not poison the configured
        # state mount's identity check.
        entries.append(
            (
                os.path.normpath(mountpoint),
                fields[3],
                fields[separator + 2],
            )
        )
    return entries


def check_state_mount_identity(
    path: Path,
    *,
    expected_source: str | None,
    expected_root: str | None,
    mountinfo_path: Path = _DEFAULT_MOUNTINFO,
) -> None:
    """Verify one configured state directory against its expected mount entry.

    No expectation means no check.  Once opted in, the target must be an exact,
    unique mountpoint and every configured identity field must match.  The
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
        entries = _mount_entries(text)
    except (OSError, UnicodeError, ValueError) as exc:
        raise StateMountIdentityError("mount identity evidence is unavailable") from exc

    target = os.path.normpath(str(path))
    matches = [entry for entry in entries if entry[0] == target]
    if not matches:
        raise StateMountIdentityError("expected state mountpoint is missing")
    if len(matches) != 1:
        raise StateMountIdentityError("state mountpoint identity is ambiguous")

    _mountpoint, raw_root, raw_source = matches[0]
    try:
        actual_root = _decode_mountinfo_field(raw_root)
        actual_source = _decode_mountinfo_field(raw_source)
        if not actual_root.startswith("/"):
            raise ValueError("relative mount root")
        actual_root = os.path.normpath(actual_root)
    except ValueError as exc:
        raise StateMountIdentityError("mount identity evidence is unavailable") from exc

    if expected_source is not None and actual_source != expected_source:
        raise StateMountIdentityError("state mount identity mismatch")
    if expected_root is not None and actual_root != expected_root:
        raise StateMountIdentityError("state mount identity mismatch")
