#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess

PREFIX = "IRLIGHT_HOST_CLOCK_SYNC"
DEFAULT_TIMEOUT_SECONDS = 3
MAX_TIMEOUT_SECONDS = 60


def emit(status: str, reason: str, *, synchronized: str | None = None) -> int:
    fields = [PREFIX, f"status={status}", f"reason={reason}"]
    if synchronized is not None:
        fields.append(f"ntp_synchronized={synchronized}")
    print(" ".join(fields))
    return {"OK": 0, "WARNING": 1, "UNKNOWN": 3}[status]


def parse_timeout() -> int | None:
    raw = os.getenv("IRLIGHT_CLOCK_SYNC_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    if not raw.isascii() or not raw.isdecimal():
        return None
    value = int(raw, 10)
    if not 1 <= value <= MAX_TIMEOUT_SECONDS:
        return None
    return value


def main() -> int:
    timeout = parse_timeout()
    if timeout is None:
        return emit("UNKNOWN", "invalid_timeout_configuration")

    executable = os.getenv("IRLIGHT_TIMEDATECTL_BIN", "timedatectl")
    if not executable or "\x00" in executable:
        return emit("UNKNOWN", "timedatectl_unavailable")

    env = dict(os.environ)
    env["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            [executable, "show", "--property=NTPSynchronized", "--value"],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return emit("UNKNOWN", "timedatectl_timeout")
    except (FileNotFoundError, PermissionError, OSError):
        return emit("UNKNOWN", "timedatectl_unavailable")

    if result.returncode != 0:
        return emit("UNKNOWN", "timedatectl_failed")

    lines = [line.strip().lower() for line in result.stdout.splitlines() if line.strip()]
    if lines == ["yes"]:
        return emit("OK", "none", synchronized="yes")
    if lines == ["no"]:
        return emit("WARNING", "ntp_unsynchronized", synchronized="no")
    return emit("UNKNOWN", "invalid_timedatectl_output")


if __name__ == "__main__":
    raise SystemExit(main())
