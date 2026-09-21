#!/usr/bin/env python3
"""Extract a bounded, allowlisted fingerprint from faulthandler output.

This helper is intentionally lossy. It never copies arbitrary traceback text,
source lines, paths, thread IDs, exception messages, or process output into its
result. Only IRLight-managed egress Python basenames, safe function names, and
line numbers are retained so a CI failure artifact can distinguish blocking
boundaries without becoming a second raw-log surface.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field


ALLOWED_FILES = {
    "destination_guard.py",
    "egress.py",
    "egress_entrypoint.py",
    "egress_policy.py",
    "egress_runtime_secret_file.py",
    "rtmp_session.py",
    "rtmp_sink.py",
    "runtime_timer_config.py",
    "secret_inputs.py",
    "stack_signal.py",
}
MAX_THREADS = 4
MAX_FRAMES_PER_THREAD = 8
MAX_OUTPUT_BYTES = 1024
UNAVAILABLE = "IRLIGHT_EGRESS_STACK_FINGERPRINT stack_fingerprint=UNAVAILABLE capped=no"

_THREAD_RE = re.compile(r"(?:Current thread|Thread) 0x[0-9A-Fa-f]+")
_FRAME_RE = re.compile(
    r'^\s*(?:[^|\n]*\|\s*)?File "[^"\n]*/([^/"\n]+)", line ([1-9][0-9]{0,6}) in ([A-Za-z_][A-Za-z0-9_]{0,79}|<module>)\s*$'
)


@dataclass
class ThreadFrames:
    frames: list[str] = field(default_factory=list)


def _format(threads: list[ThreadFrames], capped: bool) -> str:
    usable = [thread for thread in threads if thread.frames]
    if not usable:
        return UNAVAILABLE

    def render() -> str:
        groups = []
        for index, thread in enumerate(usable):
            groups.append(f"t{index}[{'>' .join(thread.frames)}]")
        return (
            "IRLIGHT_EGRESS_STACK_FINGERPRINT "
            f"threads={len(usable)} frames={';'.join(groups)} capped={'yes' if capped else 'no'}"
        )

    result = render()
    while len(result.encode("utf-8")) > MAX_OUTPUT_BYTES:
        capped = True
        removable = next((thread for thread in reversed(usable) if thread.frames), None)
        if removable is None:
            return UNAVAILABLE.replace("capped=no", "capped=yes")
        removable.frames.pop()
        usable = [thread for thread in usable if thread.frames]
        if not usable:
            return UNAVAILABLE.replace("capped=no", "capped=yes")
        result = render()
    return result


def extract(lines: list[str]) -> str:
    threads: list[ThreadFrames] = []
    current: ThreadFrames | None = None
    capped = False

    for line in lines:
        if _THREAD_RE.search(line):
            if len(threads) >= MAX_THREADS:
                current = None
                capped = True
            else:
                current = ThreadFrames()
                threads.append(current)
            continue

        if current is None:
            continue
        match = _FRAME_RE.match(line)
        if not match:
            continue
        basename, line_number, function = match.groups()
        if basename not in ALLOWED_FILES:
            continue
        if len(current.frames) >= MAX_FRAMES_PER_THREAD:
            capped = True
            continue
        current.frames.append(f"{basename}:{function}:{line_number}")

    return _format(threads, capped)


def main() -> int:
    # Bound input as well as output. A normal faulthandler dump is far smaller;
    # refusing excess bytes prevents a malformed CI log from making this
    # diagnostic helper an unbounded memory consumer.
    raw = sys.stdin.buffer.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raw = raw[: 256 * 1024]
    text = raw.decode("utf-8", errors="replace")
    print(extract(text.splitlines()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
