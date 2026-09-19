"""Pure Linux mountinfo parsing and identity comparison helpers.

The module deliberately performs no filesystem I/O. Callers decide how to read
``/proc/self/mountinfo`` and how to map parse/selection failures into their own
public status contracts.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
_ALLOWED_ESCAPES = {"011", "012", "040", "134"}


class MountInfoError(ValueError):
    """Raised when mountinfo evidence or an expected identity is malformed."""


@dataclass(frozen=True)
class MountInfoEntry:
    mountpoint: str
    raw_root: str
    raw_source: str


def decode_mountinfo_field(raw: str) -> str:
    """Decode the escape sequences Linux permits in mountinfo path-like fields."""
    cursor = 0
    decoded: list[str] = []
    for match in _ESCAPE_RE.finditer(raw):
        literal = raw[cursor : match.start()]
        if "\\" in literal or match.group(1) not in _ALLOWED_ESCAPES:
            raise MountInfoError("invalid mountinfo escape")
        decoded.append(literal)
        decoded.append(chr(int(match.group(1), 8)))
        cursor = match.end()
    tail = raw[cursor:]
    if "\\" in tail:
        raise MountInfoError("invalid mountinfo escape")
    decoded.append(tail)
    return "".join(decoded)


def parse_mountinfo_entries(text: str) -> list[MountInfoEntry]:
    """Parse mountpoint identity evidence while keeping root/source opaque.

    Root and source are intentionally not decoded here. Presence-only consumers
    must not start failing because an unrelated mount has an unusual identity
    field. Those fields are decoded only after an exact target entry is chosen.
    """
    if not text:
        raise MountInfoError("empty mountinfo")

    entries: list[MountInfoEntry] = []
    for raw_line in text.splitlines():
        if not raw_line:
            raise MountInfoError("empty mountinfo record")
        fields = raw_line.split()
        if len(fields) < 10:
            raise MountInfoError("short mountinfo record")
        try:
            separator = fields.index("-", 6)
        except ValueError as exc:
            raise MountInfoError("missing mountinfo separator") from exc
        if separator + 3 >= len(fields):
            raise MountInfoError("short mountinfo suffix")

        mountpoint = decode_mountinfo_field(fields[4])
        if not mountpoint.startswith("/"):
            raise MountInfoError("relative mountpoint")
        entries.append(
            MountInfoEntry(
                mountpoint=os.path.normpath(mountpoint),
                raw_root=fields[3],
                raw_source=fields[separator + 2],
            )
        )
    return entries


def select_exact_mounts(
    entries: list[MountInfoEntry],
    target: str | Path,
) -> list[MountInfoEntry]:
    """Return all exact mountpoint records for ``target`` in source order."""
    normalized = os.path.normpath(str(target))
    return [entry for entry in entries if entry.mountpoint == normalized]


def normalize_expected_identity(
    source: str | None,
    root: str | None,
) -> tuple[str | None, str | None]:
    """Validate and normalize an opt-in expected source/root tuple."""
    if source is not None and (not source or "\x00" in source):
        raise MountInfoError("invalid expected mount identity")

    normalized_root: str | None = None
    if root is not None:
        if not root or "\x00" in root:
            raise MountInfoError("invalid expected mount identity")
        root_path = Path(root)
        if not root_path.is_absolute():
            raise MountInfoError("invalid expected mount identity")
        normalized_root = os.path.normpath(str(root_path))

    if source is None and normalized_root is None:
        raise MountInfoError("invalid expected mount identity")
    return source, normalized_root


def identity_matches(
    entry: MountInfoEntry,
    *,
    expected_source: str | None,
    expected_root: str | None,
) -> bool:
    """Compare one selected record with a caller-validated expectation.

    Both actual identity fields are decoded whenever identity checking is
    enabled, matching the existing fail-closed consumer contract even if only
    one expected field is configured. Validation of expected values is kept
    separate so each consumer preserves its existing public error mapping.
    """
    actual_root = decode_mountinfo_field(entry.raw_root)
    actual_source = decode_mountinfo_field(entry.raw_source)
    if not actual_root.startswith("/"):
        raise MountInfoError("relative mount root")
    actual_root = os.path.normpath(actual_root)

    if expected_source is not None and actual_source != expected_source:
        return False
    if expected_root is not None and actual_root != expected_root:
        return False
    return True
