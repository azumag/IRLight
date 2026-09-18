"""Secret file helpers shared by continuity (no GStreamer import here)."""

from __future__ import annotations

import math
import os
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit


MAX_SECRET_FILE_BYTES = 64 * 1024


def _read_secret_file(path: Path, *, file_env: str) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK

    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"secret file must be a regular file: {file_env}")
        if metadata.st_size > MAX_SECRET_FILE_BYTES:
            raise RuntimeError(f"secret file exceeds size limit: {file_env}")

        payload = bytearray()
        while len(payload) <= MAX_SECRET_FILE_BYTES:
            chunk = os.read(fd, min(8192, MAX_SECRET_FILE_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > MAX_SECRET_FILE_BYTES:
            raise RuntimeError(f"secret file exceeds size limit: {file_env}")
        try:
            return bytes(payload).decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"secret file is not valid UTF-8: {file_env}") from exc
    finally:
        os.close(fd)


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
        if not math.isfinite(wait_seconds):
            wait_seconds = 60.0
        # Cap to 5 minutes to prevent a single env var from stalling startup indefinitely.
        wait_seconds = min(max(0.0, wait_seconds), 300.0)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                value = _read_secret_file(Path(file_path), file_env=file_env)
            except RuntimeError:
                raise
            except OSError as exc:
                if time.monotonic() < deadline:
                    time.sleep(0.1)
                    continue
                raise RuntimeError(f"cannot read secret file: {file_env}") from exc
            if value:
                return value
            if time.monotonic() >= deadline:
                raise RuntimeError(f"empty secret file: {file_env}")
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
