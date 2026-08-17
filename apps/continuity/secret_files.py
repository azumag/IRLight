"""Secret file helpers shared by continuity (no GStreamer import here)."""

from __future__ import annotations

import os
from pathlib import Path


def read_secret_file_or_env(name: str, default: str) -> str:
    """Prefer a secret file (e.g. EGRESS_URL_FILE) over an env var.

    The production compose delivers destination secrets through tmpfs files so
    they never appear in ``docker inspect`` output or process arguments.
    """
    file_env = f"{name}_FILE"
    file_path = os.getenv(file_env)
    if file_path:
        try:
            value = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"cannot read {file_env}={file_path}: {exc}") from exc
        if value:
            return value
    return os.getenv(name, default)
