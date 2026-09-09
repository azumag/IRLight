"""Shared fail-closed JSONL primitives for read-only operations tooling."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, BinaryIO, TextIO


class DuplicateKeyError(ValueError):
    pass


class NonFiniteNumberError(ValueError):
    pass


class OversizedRecord:
    pass


class InvalidUtf8Record:
    pass


OVERSIZED_RECORD = OversizedRecord()
INVALID_UTF8_RECORD = InvalidUtf8Record()


def reject_nonfinite(value: str) -> None:
    raise NonFiniteNumberError(value)


def object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(key)
        result[key] = value
    return result


def iter_bounded_lines(
    stream: TextIO, *, max_record_bytes: int
) -> Iterable[str | OversizedRecord]:
    """Bound text input for tests/in-process callers.

    ``TextIO.readline`` bounds characters rather than UTF-8 bytes, so callers
    must still apply :func:`utf8_size_violation` before parsing. CLI entrypoints
    should prefer :func:`iter_bounded_byte_lines` when a raw buffer is available.
    """
    if max_record_bytes <= 0:
        raise ValueError("max_record_bytes must be positive")
    read_limit = max_record_bytes + 1
    while True:
        line = stream.readline(read_limit)
        if line == "":
            return
        if line.endswith("\n") or len(line) < read_limit:
            yield line
            continue
        while line and not line.endswith("\n"):
            line = stream.readline(read_limit)
        yield OVERSIZED_RECORD


def iter_bounded_byte_lines(
    stream: BinaryIO, *, max_record_bytes: int
) -> Iterable[str | OversizedRecord | InvalidUtf8Record]:
    """Apply a record byte cap before UTF-8 decoding and drain oversized lines."""
    if max_record_bytes <= 0:
        raise ValueError("max_record_bytes must be positive")
    read_limit = max_record_bytes + 1
    while True:
        line = stream.readline(read_limit)
        if line == b"":
            return
        if line.endswith(b"\n") or len(line) < read_limit:
            if len(line) > max_record_bytes:
                yield OVERSIZED_RECORD
                continue
            try:
                yield line.decode("utf-8")
            except UnicodeDecodeError:
                yield INVALID_UTF8_RECORD
            continue
        while line and not line.endswith(b"\n"):
            line = stream.readline(read_limit)
        yield OVERSIZED_RECORD


def utf8_size_violation(line: str, *, max_record_bytes: int) -> str | None:
    """Return a stable violation code when a text record is not safely bounded."""
    try:
        record_bytes = len(line.encode("utf-8"))
    except UnicodeEncodeError:
        return "INVALID_JSON"
    if record_bytes > max_record_bytes:
        return "RECORD_TOO_LARGE"
    return None


def strict_json_loads(line: str) -> Any:
    """Decode JSON while rejecting duplicate object keys and non-finite numbers."""
    return json.loads(
        line,
        object_pairs_hook=object_without_duplicates,
        parse_constant=reject_nonfinite,
    )
