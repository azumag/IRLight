"""Singleton wiring for the Session API (spike).

The control plane keeps one in-memory fake provider and one session store per
process so the API, workflow and reaper share state. In production this would
be replaced with the real ConoHa provider and a durable database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


# Local checkouts run with cwd inside apps/control-api; add the repo root so
# `provider` imports resolve. In the container the Dockerfile sets
# PYTHONPATH=/app and provider/ is copied alongside.
try:
    _LOCAL_REPO = Path(__file__).resolve().parents[2]
except IndexError:
    _LOCAL_REPO = None
if _LOCAL_REPO is not None and (_LOCAL_REPO / "provider").is_dir():
    if str(_LOCAL_REPO) not in sys.path:
        sys.path.insert(0, str(_LOCAL_REPO))

from provider.fake_provider import FakeProvider  # noqa: E402

from session_store import SessionStore  # noqa: E402


_STORE: SessionStore | None = None
_PROVIDER: FakeProvider | None = None


def default_store() -> SessionStore:
    global _STORE
    if _STORE is None:
        _STORE = SessionStore(os.getenv("STATE_DIR", "/state"))
    return _STORE


def default_provider() -> FakeProvider:
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = FakeProvider()
    return _PROVIDER
