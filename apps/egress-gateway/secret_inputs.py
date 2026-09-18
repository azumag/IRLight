"""Credential-bearing Egress Gateway input readers.

This module stays independent from GStreamer so the secret-file boundary can be
unit tested without importing the media runtime.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from runtime_secret_file import RuntimeSecretFileError, read_runtime_secret


_DEFAULT_INPUT_URI = "rtsp://mediamtx:8554/output/relay"


def _secret_wait_seconds() -> float:
    try:
        wait_seconds = float(os.getenv("IRLIGHT_SECRET_WAIT_SECONDS", "60"))
    except ValueError:
        wait_seconds = 60.0
    return min(max(0.0, wait_seconds), 300.0)


def read_input_uri() -> str:
    """Read and validate the RTSP input URI while preserving startup wait semantics."""

    path_value = os.getenv("EGRESS_INPUT_URI_FILE", "").strip()
    if not path_value:
        return os.getenv("EGRESS_INPUT_URI", _DEFAULT_INPUT_URI)

    deadline = time.monotonic() + _secret_wait_seconds()
    path = Path(path_value)
    while True:
        try:
            value = read_runtime_secret(path).strip()
        except RuntimeSecretFileError:
            if time.monotonic() < deadline:
                time.sleep(0.1)
                continue
            raise RuntimeError("egress input secret file is unavailable") from None

        parsed = urlsplit(value)
        if parsed.scheme.lower() == "rtsp" and parsed.hostname:
            return value
        if time.monotonic() >= deadline:
            raise RuntimeError("egress input URL is invalid") from None
        time.sleep(0.1)


def read_destination_url(path: Path) -> str:
    """Read and validate a credential-bearing RTMP/RTMPS destination URL."""

    try:
        value = read_runtime_secret(path).strip()
    except RuntimeSecretFileError:
        raise RuntimeError("egress destination secret file is unavailable") from None

    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"rtmp", "rtmps"} or not parsed.hostname:
        raise RuntimeError("egress destination URL is invalid") from None
    return value
