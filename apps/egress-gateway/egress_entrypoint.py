from __future__ import annotations

import logging
import os
from pathlib import Path

import egress
from rtmp_sink import destination_url_for_sink, parse_rtmp_sink_factory


LOG = logging.getLogger("irlight.egress.entrypoint")
_original_read_destination_url = egress.read_destination_url


def _read_destination_url(path: Path) -> str:
    url = _original_read_destination_url(path)
    try:
        sink_factory = parse_rtmp_sink_factory(os.getenv("EGRESS_RTMP_SINK_FACTORY"))
        return destination_url_for_sink(
            url,
            sink_factory=sink_factory,
            librtmp_timeout_raw=os.getenv("EGRESS_LIBRTMP_SESSION_TIMEOUT_SECONDS"),
        )
    except ValueError:
        # Never log the URL: it may contain the destination stream key.
        LOG.error("invalid RTMP sink or timeout configuration")
        raise RuntimeError("egress destination URL is invalid") from None


def main() -> int:
    egress.read_destination_url = _read_destination_url
    return egress.main()


if __name__ == "__main__":
    raise SystemExit(main())
