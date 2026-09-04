"""Observe the authenticated output relay without exposing client identity."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RelayClientConfig:
    api_url: str = "http://mediamtx:9997"
    path: str = "output/relay"
    timeout_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> "RelayClientConfig":
        try:
            timeout_seconds = float(
                os.getenv("NODE_RELAY_CLIENT_API_TIMEOUT_SECONDS", "2")
            )
        except ValueError:
            timeout_seconds = 2.0
        return cls(
            api_url=os.getenv(
                "NODE_MEDIAMTX_API_URL",
                "http://mediamtx:9997",
            ).rstrip("/"),
            path=os.getenv("NODE_RELAY_PATH", "output/relay"),
            timeout_seconds=min(max(timeout_seconds, 0.2), 10.0),
        )


class RelayClientObserver:
    """Map MediaMTX's anonymous reader count to a safe connection state."""

    def __init__(self, config: RelayClientConfig | None = None) -> None:
        self.config = config or RelayClientConfig.from_env()

    def _request_json(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self.config.api_url + path,
            headers={"Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.config.timeout_seconds,
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            raise RuntimeError(f"MediaMTX API HTTP {exc.code}: {body[:160]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"MediaMTX API unavailable: {exc.reason}") from exc
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("MediaMTX API returned invalid JSON") from exc
        return value if isinstance(value, dict) else {}

    def _path_snapshot(self) -> dict[str, Any] | None:
        payload = self._request_json("/v3/paths/list?itemsPerPage=100")
        items = payload.get("items", [])
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict) and item.get("name") == self.config.path:
                return item
        return None

    def observe(self, *, now: float | None = None) -> dict[str, Any]:
        observed_at = time.time() if now is None else now
        base = {
            "status": "UNKNOWN",
            "connected": False,
            "reader_count": 0,
            "reason_code": None,
            "observed_at": observed_at,
        }
        try:
            path = self._path_snapshot()
        except RuntimeError as exc:
            return {**base, "reason_code": str(exc)[:100]}

        if path is None or path.get("online") is not True:
            return {**base, "reason_code": "RELAY_SOURCE_OFFLINE"}

        readers = path.get("readers", [])
        if readers is None:
            readers = []
        if not isinstance(readers, list):
            return {**base, "reason_code": "READER_STATE_UNAVAILABLE"}

        return {
            **base,
            "status": "CONNECTED" if readers else "DISCONNECTED",
            "connected": bool(readers),
            "reader_count": len(readers),
            "reason_code": None,
        }
