from __future__ import annotations

import logging
import os
from pathlib import Path

import egress
from rtmp_session import parse_librtmp_session_timeout, with_librtmp_session_timeout


LOG = logging.getLogger("irlight.egress.entrypoint")
_original_read_destination_url = egress.read_destination_url


def _read_destination_url(path: Path) -> str:
    url = _original_read_destination_url(path)
    try:
        timeout_seconds = parse_librtmp_session_timeout(
            os.getenv("EGRESS_LIBRTMP_SESSION_TIMEOUT_SECONDS")
        )
        return with_librtmp_session_timeout(url, timeout_seconds)
    except ValueError:
        # Never log the URL: it may contain the destination stream key.
        LOG.error("invalid librtmp session timeout configuration or destination URL")
        raise RuntimeError("egress destination URL is invalid") from None


def main() -> int:
    egress.read_destination_url = _read_destination_url
    return egress.main()


if __name__ == "__main__":
    raise SystemExit(main())
