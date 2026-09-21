from __future__ import annotations

import faulthandler
import json
import math
import sys
import threading
import time
from pathlib import Path
from typing import Callable, TextIO


STACK_MARKER = "IRLIGHT_EGRESS_STALE_STATUS_STACK"


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON constant")


def _finite_timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


class ConnectedStatusStackWatchdog:
    """Dump Python stacks once when CONNECTED status stops refreshing.

    The dump is deliberately metadata-only: ``faulthandler`` prints file names,
    line numbers and function names, but not local variable values.  This makes
    it suitable for diagnosing a blocked GStreamer/librtmp call without
    reflecting the credentialed destination URL or stream key into logs.
    """

    def __init__(
        self,
        status_file: Path,
        *,
        heartbeat_seconds: float,
        started_at: float | None = None,
        now: Callable[[], float] = time.time,
        dump_traceback: Callable[..., None] = faulthandler.dump_traceback,
        output: TextIO | None = None,
    ) -> None:
        self.status_file = status_file
        self.heartbeat_seconds = max(0.0, heartbeat_seconds)
        self.stale_seconds = max(15.0, self.heartbeat_seconds * 3.0)
        self.poll_seconds = max(0.5, min(2.0, self.heartbeat_seconds or 2.0))
        self.started_at = now() if started_at is None else started_at
        self._now = now
        self._dump_traceback = dump_traceback
        self._output = output if output is not None else sys.stderr
        self._last_dumped_observed_at: float | None = None

    @property
    def enabled(self) -> bool:
        # A zero heartbeat intentionally disables periodic status refreshes, so
        # age alone cannot distinguish a healthy connection from a blocked one.
        return self.heartbeat_seconds > 0

    def _load_status(self) -> dict[str, object] | None:
        try:
            value = json.loads(
                self.status_file.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def inspect_once(self) -> bool:
        """Inspect one snapshot and return True only when a stack was dumped."""
        if not self.enabled:
            return False
        value = self._load_status()
        if value is None:
            return False
        if value.get("status") != "CONNECTED" or value.get("connected") is not True:
            return False

        observed_at = _finite_timestamp(value.get("observed_at"))
        if observed_at is None or observed_at < self.started_at:
            # Do not diagnose a stale CONNECTED record left by a previous
            # process generation before this entrypoint had a chance to write
            # STARTING/current status.
            return False

        now = self._now()
        if not math.isfinite(now) or observed_at > now:
            return False
        if now - observed_at < self.stale_seconds:
            return False
        if self._last_dumped_observed_at == observed_at:
            return False

        self._last_dumped_observed_at = observed_at
        print(
            f"{STACK_MARKER} status=CONNECTED age_bucket=stale",
            file=self._output,
            flush=True,
        )
        self._dump_traceback(file=self._output, all_threads=True)
        return True

    def run_forever(self) -> None:
        if not self.enabled:
            return
        while True:
            try:
                self.inspect_once()
            except Exception:
                # Diagnostics must never take down or perturb the media path.
                pass
            time.sleep(self.poll_seconds)

    def start(self) -> threading.Thread | None:
        if not self.enabled:
            return None
        thread = threading.Thread(
            target=self.run_forever,
            name="egress-stale-status-watchdog",
            daemon=True,
        )
        thread.start()
        return thread
