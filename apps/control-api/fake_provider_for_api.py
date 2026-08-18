"""Runtime provider and SessionStore wiring for the Control Plane.

``IRLIGHT_PROVIDER`` selects the provider implementation:

- ``fake`` (default): in-memory provider used by local/CI smoke tests.
- ``conoha``: real ConoHa REST client configured through ``CONOHA_*`` env vars.

The fake provider remains a process singleton because its remote state is
in-memory. The ConoHa client is created per workflow/reaper invocation so token
state is not shared across concurrent API requests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


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
from provider.provider_client import ConohaClient, ConohaConfig  # noqa: E402

from session_store import SessionStore  # noqa: E402


_STORE: SessionStore | None = None
_FAKE_PROVIDER: FakeProvider | None = None


def default_store() -> SessionStore:
    global _STORE
    if _STORE is None:
        _STORE = SessionStore(os.getenv("STATE_DIR", "/state"))
    return _STORE


def provider_mode() -> str:
    mode = os.getenv("IRLIGHT_PROVIDER", "fake").strip().lower()
    if mode not in {"fake", "conoha"}:
        raise RuntimeError(
            f"unsupported IRLIGHT_PROVIDER={mode!r}; expected 'fake' or 'conoha'"
        )
    return mode


def default_provider() -> Any:
    """Return the provider selected for this process invocation.

    FakeProvider is intentionally shared inside the process. ConoHa is backed
    by the remote API, therefore each caller gets a fresh lightweight client.
    """

    global _FAKE_PROVIDER
    if provider_mode() == "conoha":
        return ConohaClient(ConohaConfig.from_env())

    if _FAKE_PROVIDER is None:
        _FAKE_PROVIDER = FakeProvider()
    return _FAKE_PROVIDER
