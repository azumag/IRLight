"""Standalone reaper entrypoint.

Run periodically (cron / systemd timer) against the same STATE_DIR as the
control plane. It cleans up timed-out sessions, orphaned provider resources,
and expired authentication sessions without touching the media plane.
"""

from __future__ import annotations

import argparse
import logging
import os
import time

from auth_session_gc import DEFAULT_MAX_DELETIONS, prune_expired_sessions
from auth_store import AuthStateError
from fake_provider_for_api import default_provider, default_store
from reaper import Reaper, ReaperConfig


LOG = logging.getLogger("irlight.reaper_cli")


def _elapsed_ms(started_at: float) -> int:
    """Return a non-negative aggregate duration without exposing state data."""

    return max(0, round((time.monotonic() - started_at) * 1000))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reaper_cli")
    parser.add_argument(
        "--provisioning-timeout-seconds", type=float, default=600.0
    )
    parser.add_argument("--no-ingest-timeout-seconds", type=float, default=3600.0)
    parser.add_argument("--hold-timeout-seconds", type=float, default=1800.0)
    parser.add_argument(
        "--heartbeat-grace-seconds",
        type=float,
        default=float(os.getenv("NODE_HEARTBEAT_GRACE_SECONDS", "120")),
    )
    parser.add_argument(
        "--auth-session-gc-max-delete",
        type=int,
        default=DEFAULT_MAX_DELETIONS,
        help="maximum expired authentication sessions to delete per sweep",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    store = default_store()
    provider = default_provider()
    reaper = Reaper(
        store,
        provider,
        ReaperConfig(
            provisioning_timeout_seconds=args.provisioning_timeout_seconds,
            no_ingest_timeout_seconds=args.no_ingest_timeout_seconds,
            hold_timeout_seconds=args.hold_timeout_seconds,
            heartbeat_grace_seconds=args.heartbeat_grace_seconds,
        ),
    )
    result = reaper.run()

    # Provider/session cleanup must not be skipped because authentication state
    # is damaged. Run the bounded auth-session GC afterwards and surface a fixed
    # reason code instead of leaking authority paths or records.
    auth_gc_started_at = time.monotonic()
    try:
        auth_gc = prune_expired_sessions(
            max_deletions=args.auth_session_gc_max_delete
        )
    except (AuthStateError, ValueError):
        LOG.error(
            "authentication session GC failed; inspect authentication authority state"
        )
        result["auth_session_gc_status"] = "failed"
        result["auth_session_gc_reason"] = "AUTH_SESSION_GC_FAILED"
        result["auth_session_gc_elapsed_ms"] = _elapsed_ms(auth_gc_started_at)
        print(result)
        return 1

    # Keep the existing flat dict output contract used by cleanup smoke tests.
    result["auth_session_gc_status"] = "ok"
    result["auth_session_gc_scanned"] = auth_gc.scanned
    result["auth_session_gc_deleted"] = auth_gc.deleted
    result["auth_session_gc_expired_remaining"] = auth_gc.expired_remaining
    result["auth_session_gc_elapsed_ms"] = _elapsed_ms(auth_gc_started_at)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
