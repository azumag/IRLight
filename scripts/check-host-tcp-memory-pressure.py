#!/usr/bin/env python3
"""Read-only Linux TCP memory-pressure diagnostic.

The checker compares the current TCP memory page count from /proc/net/sockstat
with the kernel's tcp_mem pressure/max watermarks. It never changes sysctls,
sockets, routes, services, or provider state.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

MAX_INPUT_BYTES = 64 * 1024


class InputError(RuntimeError):
    """Raised when kernel diagnostic input cannot be trusted."""


def _read_bounded(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise InputError("input_not_regular")
        payload = bytearray()
        while len(payload) <= MAX_INPUT_BYTES:
            chunk = os.read(fd, min(4096, MAX_INPUT_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > MAX_INPUT_BYTES:
            raise InputError("input_too_large")
        try:
            return payload.decode("ascii")
        except UnicodeDecodeError as exc:
            raise InputError("input_not_ascii") from exc
    except InputError:
        raise
    except OSError as exc:
        raise InputError("input_unavailable") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _parse_nonnegative_decimal(raw: str, *, reason: str) -> int:
    if not raw or not raw.isascii() or not raw.isdigit() or len(raw) > 20:
        raise InputError(reason)
    value = int(raw, 10)
    if value < 0 or value > (2**63 - 1):
        raise InputError(reason)
    return value


def parse_tcp_mem_pages(sockstat_text: str) -> int:
    tcp_records = [line for line in sockstat_text.splitlines() if line.startswith("TCP:")]
    if len(tcp_records) != 1:
        raise InputError("sockstat_tcp_record_invalid")
    fields = tcp_records[0].split()
    if len(fields) < 3 or fields[0] != "TCP:" or (len(fields) - 1) % 2 != 0:
        raise InputError("sockstat_tcp_record_invalid")
    parsed: dict[str, int] = {}
    for index in range(1, len(fields), 2):
        key = fields[index]
        if key in parsed:
            raise InputError("sockstat_tcp_record_invalid")
        parsed[key] = _parse_nonnegative_decimal(
            fields[index + 1], reason="sockstat_tcp_record_invalid"
        )
    if "mem" not in parsed:
        raise InputError("sockstat_tcp_mem_missing")
    return parsed["mem"]


def parse_tcp_mem_watermarks(tcp_mem_text: str) -> tuple[int, int, int]:
    fields = tcp_mem_text.split()
    if len(fields) != 3:
        raise InputError("tcp_mem_watermarks_invalid")
    low, pressure, high = (
        _parse_nonnegative_decimal(field, reason="tcp_mem_watermarks_invalid")
        for field in fields
    )
    if low <= 0 or pressure <= 0 or high <= 0 or not (low <= pressure <= high):
        raise InputError("tcp_mem_watermarks_invalid")
    return low, pressure, high


def classify(mem_pages: int, pressure_pages: int, high_pages: int) -> tuple[str, int, str]:
    if mem_pages >= high_pages:
        return "CRITICAL", 2, "at_or_above_max"
    if mem_pages >= pressure_pages:
        return "WARNING", 1, "at_or_above_pressure"
    return "OK", 0, "below_pressure"


def _unknown(reason: str) -> int:
    print(
        "IRLIGHT_TCP_MEMORY_PRESSURE "
        f"status=UNKNOWN tcp_mem_pages=NA pressure_pages=NA max_pages=NA reason={reason}"
    )
    return 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sockstat", default="/proc/net/sockstat")
    parser.add_argument("--tcp-mem", default="/proc/sys/net/ipv4/tcp_mem")
    args = parser.parse_args(argv)

    try:
        mem_pages = parse_tcp_mem_pages(_read_bounded(Path(args.sockstat)))
        _low_pages, pressure_pages, high_pages = parse_tcp_mem_watermarks(
            _read_bounded(Path(args.tcp_mem))
        )
    except InputError as exc:
        return _unknown(str(exc))

    status, exit_code, reason = classify(mem_pages, pressure_pages, high_pages)
    print(
        "IRLIGHT_TCP_MEMORY_PRESSURE "
        f"status={status} tcp_mem_pages={mem_pages} "
        f"pressure_pages={pressure_pages} max_pages={high_pages} reason={reason}"
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
