"""Read-only ingest connectivity inspection for Media Node operators.

The inspector performs a single GET against the node-internal MediaMTX path API
and emits a small redacted summary. It never calls a public ingest endpoint,
changes MediaMTX state, kicks publishers, prints source IDs, or echoes API error
bodies/URLs that could contain deployment details.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import urllib.error
import urllib.request
from typing import Any


MAX_RESPONSE_BYTES = 1024 * 1024
SOURCE_PROTOCOLS = {
    "rtmpConn": "RTMP",
    "rtmpsConn": "RTMPS",
    "srtConn": "SRT",
}


class IngestFailureInspectError(RuntimeError):
    """Raised when ingest state cannot be inspected safely."""


def _bounded_timeout(value: str) -> float:
    try:
        numeric = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(numeric) or numeric < 0.2 or numeric > 10.0:
        raise argparse.ArgumentTypeError("must be between 0.2 and 10 seconds")
    return numeric


def _request_paths(*, api_url: str, timeout_seconds: float) -> dict[str, Any]:
    try:
        request = urllib.request.Request(
            api_url.rstrip("/") + "/v3/paths/list?itemsPerPage=100",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (
        ValueError,
        urllib.error.HTTPError,
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        OSError,
    ) as exc:
        raise IngestFailureInspectError("MediaMTX path API is unavailable") from exc

    if len(raw) > MAX_RESPONSE_BYTES:
        raise IngestFailureInspectError("MediaMTX path API response is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngestFailureInspectError("MediaMTX path API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise IngestFailureInspectError("MediaMTX path API returned invalid structure")
    return payload


def summarize_paths(payload: dict[str, Any], *, ingest_path: str) -> dict[str, Any]:
    items = payload.get("items")
    if not isinstance(items, list):
        raise IngestFailureInspectError("MediaMTX path API omitted path items")

    target: dict[str, Any] | None = None
    for item in items:
        if not isinstance(item, dict):
            raise IngestFailureInspectError("MediaMTX path API returned invalid path item")
        if item.get("name") == ingest_path:
            target = item
            break

    if target is None:
        return {
            "status": "OFFLINE",
            "reason": "INGEST_PATH_NOT_VISIBLE",
            "online": False,
            "source_protocol": None,
            "track_count": 0,
        }

    online = target.get("online")
    if not isinstance(online, bool):
        raise IngestFailureInspectError("MediaMTX path API returned invalid online state")
    if not online:
        return {
            "status": "OFFLINE",
            "reason": "INGEST_NO_PUBLISHER",
            "online": False,
            "source_protocol": None,
            "track_count": 0,
        }

    source = target.get("source")
    tracks = target.get("tracks2", [])
    if not isinstance(source, dict) or not isinstance(tracks, list):
        raise IngestFailureInspectError("MediaMTX online path state is incomplete")
    source_type = source.get("type")
    source_protocol = SOURCE_PROTOCOLS.get(source_type, "OTHER")

    return {
        "status": "ONLINE",
        "reason": "INGEST_PUBLISHER_ONLINE",
        "online": True,
        "source_protocol": source_protocol,
        "track_count": len(tracks),
    }


def inspect_ingest(
    *, api_url: str, ingest_path: str, timeout_seconds: float
) -> dict[str, Any]:
    payload = _request_paths(api_url=api_url, timeout_seconds=timeout_seconds)
    return summarize_paths(payload, ingest_path=ingest_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingest_failure_inspect_cli")
    parser.add_argument(
        "--timeout-seconds",
        type=_bounded_timeout,
        default=os.getenv("NODE_MEDIAMTX_API_TIMEOUT_SECONDS", "2"),
        help="Bounded internal MediaMTX API timeout (default: env or 2)",
    )
    args = parser.parse_args(argv)

    api_url = os.getenv("NODE_MEDIAMTX_API_URL", "http://mediamtx:9997")
    ingest_path = os.getenv("NODE_INGEST_PATH", "live/input")
    if not api_url.strip() or not ingest_path.strip():
        print(
            json.dumps(
                {"status": "UNAVAILABLE", "reason": "INGEST_INSPECTION_UNAVAILABLE"},
                sort_keys=True,
            )
        )
        return 3

    try:
        result = inspect_ingest(
            api_url=api_url,
            ingest_path=ingest_path,
            timeout_seconds=args.timeout_seconds,
        )
    except IngestFailureInspectError:
        print(
            json.dumps(
                {"status": "UNAVAILABLE", "reason": "INGEST_INSPECTION_UNAVAILABLE"},
                sort_keys=True,
            )
        )
        return 3

    print(json.dumps(result, sort_keys=True))
    return 0 if result["online"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
