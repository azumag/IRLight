"""Standalone reaper entrypoint.

Run periodically (cron / systemd timer) against the same STATE_DIR as the
control plane. It cleans up timed-out sessions and orphaned provider resources
without touching the media plane.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from fake_provider_for_api import default_provider, default_store
from reaper import Reaper, ReaperConfig


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
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
