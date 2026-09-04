"""Secret file helpers shared by continuity (no GStreamer import here)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import urlsplit


def read_secret_file_or_env(name: str, default: str) -> str:
    """Prefer a secret file (e.g. EGRESS_URL_FILE) over an env var.

    Production Continuity receives only node-local authenticated media URIs
    through tmpfs files. External destination credentials remain isolated in
    the Egress Gateway and never appear in Continuity's inspect output or args.
    """
    file_env = f"{name}_FILE"
    file_path = os.getenv(file_env)
    if file_path:
        try:
            wait_seconds = float(os.getenv("IRLIGHT_SECRET_WAIT_SECONDS", "60"))
        except ValueError:
            wait_seconds = 60.0
        # Cap to 5 minutes to prevent a single env var from stalling startup indefinitely.
        wait_seconds = min(max(0.0, wait_seconds), 300.0)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                value = Path(file_path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                if time.monotonic() < deadline:
                    time.sleep(0.1)
                    continue
                raise RuntimeError(f"cannot read {file_env}={file_path}: {exc}") from exc
            if value:
                return value
            if time.monotonic() >= deadline:
                raise RuntimeError(f"empty secret file: {file_env}={file_path}")
            time.sleep(0.1)
    return os.getenv(name, default)


def redact_stream_url(url: str) -> str:
    """Return only scheme and host, never userinfo, path, query, or fragment."""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<configured>"
    if not scheme or not hostname:
        return "<configured>"
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    display_port = f":{port}" if port is not None else ""
    return f"{scheme}://{display_host}{display_port}/…"
