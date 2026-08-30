"""Node-local MediaMTX auth proxy with bounded positive-cache fallback."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


MAX_REQUEST_BYTES = 16 * 1024
CACHE_KEY_FIELDS = ("action", "path", "protocol", "user", "password", "token")
CACHED_ACTION = "publish"
INTERNAL_MEDIA_USERNAME = "irlight-internal"
INTERNAL_MEDIA_ACTIONS = {
    ("rtmp", "publish", "output/relay"),
    ("rtsp", "read", "live/input"),
    ("rtsp", "read", "output/relay"),
}


@dataclass(frozen=True)
class AuthProxyConfig:
    listen_host: str = "0.0.0.0"
    listen_port: int = 8090
    upstream_timeout_seconds: float = 2.0
    cache_max_age_seconds: float = 300.0
    cache_max_entries: int = 256

    @classmethod
    def from_env(cls) -> "AuthProxyConfig":
        return cls(
            listen_host=os.getenv("NODE_INGEST_AUTH_LISTEN_HOST", "0.0.0.0"),
            listen_port=_env_int("NODE_INGEST_AUTH_LISTEN_PORT", 8090, 1, 65535),
            upstream_timeout_seconds=_env_float(
                "NODE_INGEST_AUTH_UPSTREAM_TIMEOUT_SECONDS", 2.0, 0.1, 30.0
            ),
            cache_max_age_seconds=_env_float(
                "NODE_INGEST_AUTH_CACHE_MAX_AGE_SECONDS", 300.0, 1.0, 3600.0
            ),
            cache_max_entries=_env_int(
                "NODE_INGEST_AUTH_CACHE_MAX_ENTRIES", 256, 1, 4096
            ),
        )


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return min(maximum, max(minimum, value))


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _cache_key(payload: dict[str, Any]) -> str:
    canonical = [str(payload.get(field, "")) for field in CACHE_KEY_FIELDS]
    raw = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PositiveAuthCache:
    """Memory-only digest cache for previously authorized publisher requests."""

    def __init__(self, *, max_age_seconds: float, max_entries: int) -> None:
        self.max_age_seconds = max_age_seconds
        self.max_entries = max_entries
        self.lock = threading.Lock()
        self._entries: OrderedDict[str, float] = OrderedDict()

    def _prune(self, now: float) -> None:
        expired = [key for key, expires_at in self._entries.items() if expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def store(
        self,
        payload: dict[str, Any],
        *,
        upstream_valid_until: float,
        now: float | None = None,
    ) -> bool:
        current = time.time() if now is None else now
        expires_at = min(upstream_valid_until, current + self.max_age_seconds)
        if not math.isfinite(expires_at) or expires_at <= current:
            return False
        key = _cache_key(payload)
        with self.lock:
            self._entries[key] = expires_at
            self._entries.move_to_end(key)
            self._prune(current)
        return True

    def allowed(self, payload: dict[str, Any], *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        key = _cache_key(payload)
        with self.lock:
            self._prune(current)
            expires_at = self._entries.get(key)
            if expires_at is None or expires_at <= current:
                return False
            self._entries.move_to_end(key)
            return True

    def evict(self, payload: dict[str, Any]) -> None:
        key = _cache_key(payload)
        with self.lock:
            self._entries.pop(key, None)

    def size(self, *, now: float | None = None) -> int:
        current = time.time() if now is None else now
        with self.lock:
            self._prune(current)
            return len(self._entries)


@dataclass(frozen=True)
class ProxyResponse:
    status: int
    body: bytes
    headers: dict[str, str]


class IngestAuthProxy:
    """Forwards MediaMTX auth to Control Plane and falls back to short-lived cache."""

    def __init__(
        self,
        *,
        upstream_url: str,
        config: AuthProxyConfig | None = None,
        cache: PositiveAuthCache | None = None,
        upstream_token: str | None = None,
        internal_media_secret: str | None = None,
        internal_media_secrets: dict[tuple[str, str, str], str] | None = None,
    ) -> None:
        self.upstream_url = upstream_url
        self.upstream_token = upstream_token
        self.config = config or AuthProxyConfig.from_env()
        self.cache = cache or PositiveAuthCache(
            max_age_seconds=self.config.cache_max_age_seconds,
            max_entries=self.config.cache_max_entries,
        )
        if internal_media_secrets is not None:
            self.internal_media_secrets = dict(internal_media_secrets)
        elif internal_media_secret:
            self.internal_media_secrets = {
                action: internal_media_secret for action in INTERNAL_MEDIA_ACTIONS
            }
        else:
            self.internal_media_secrets = {}
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.server is not None:
            return
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _send(self, response: ProxyResponse) -> None:
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json")
                for name, value in response.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                self.wfile.write(response.body)

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/auth":
                    self._send(_json_response(404, {"detail": "not found"}))
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send(_json_response(400, {"detail": "invalid content length"}))
                    return
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    status = 413 if length > MAX_REQUEST_BYTES else 400
                    self._send(_json_response(status, {"detail": "invalid auth request"}))
                    return
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send(_json_response(400, {"detail": "invalid auth request"}))
                    return
                if not isinstance(payload, dict):
                    self._send(_json_response(400, {"detail": "invalid auth request"}))
                    return
                self._send(proxy.authorize(payload))

        server = ThreadingHTTPServer(
            (self.config.listen_host, self.config.listen_port), Handler
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        try:
            thread.start()
        except Exception:
            server.server_close()
            raise
        self.server = server
        self.thread = thread

    def stop(self) -> None:
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3.0)
        self.server = None
        self.thread = None

    @property
    def port(self) -> int | None:
        if self.server is None:
            return None
        return int(self.server.server_address[1])

    def authorize(self, payload: dict[str, Any]) -> ProxyResponse:
        internal = self._authorize_internal_media(payload)
        if internal is not None:
            return internal
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.upstream_token:
            headers["Authorization"] = f"Bearer {self.upstream_token}"
        request = urllib.request.Request(
            self.upstream_url,
            data=raw,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.upstream_timeout_seconds
            ) as response:
                body = response.read()
                status = int(response.status)
                retry_after = response.headers.get("Retry-After")
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
                status = int(exc.code)
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
            finally:
                exc.close()
            if 400 <= status < 500:
                self.cache.evict(payload)
                headers = {"Retry-After": retry_after} if retry_after else {}
                return ProxyResponse(status=status, body=body or b"{}", headers=headers)
            return self._fallback(payload)
        except (urllib.error.URLError, TimeoutError, OSError):
            return self._fallback(payload)

        if not 200 <= status < 300:
            self.cache.evict(payload)
            return ProxyResponse(status=status, body=body or b"{}", headers={})

        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.cache.evict(payload)
            return _json_response(502, {"detail": "invalid auth upstream response"})
        if not isinstance(result, dict) or result.get("authorized") is not True:
            self.cache.evict(payload)
            return _json_response(502, {"detail": "invalid auth upstream response"})

        try:
            cache_valid_until = float(result.get("cache_valid_until", 0.0))
        except (TypeError, ValueError):
            cache_valid_until = 0.0
        if self._cacheable(payload):
            self.cache.store(payload, upstream_valid_until=cache_valid_until)
        headers = {"Retry-After": retry_after} if retry_after else {}
        return ProxyResponse(status=status, body=body, headers=headers)

    def _authorize_internal_media(
        self, payload: dict[str, Any]
    ) -> ProxyResponse | None:
        if str(payload.get("user", "")) != INTERNAL_MEDIA_USERNAME:
            return None
        action = (
            str(payload.get("protocol", "")).lower(),
            str(payload.get("action", "")).lower(),
            str(payload.get("path", "")),
        )
        if action not in INTERNAL_MEDIA_ACTIONS:
            return _json_response(403, {"detail": "unsupported internal media action"})
        configured = self.internal_media_secrets.get(action, "")
        supplied = str(payload.get("password", ""))
        if not configured or not secrets.compare_digest(configured, supplied):
            return _json_response(401, {"detail": "invalid internal media credential"})
        return _json_response(200, {"authorized": True, "internal": True})

    def _fallback(self, payload: dict[str, Any]) -> ProxyResponse:
        if not self._cacheable(payload) or not self.cache.allowed(payload):
            return _json_response(
                503,
                {"detail": "ingest auth upstream unavailable"},
                headers={"Retry-After": "2"},
            )
        return _json_response(200, {"authorized": True, "cached": True})

    @staticmethod
    def _cacheable(payload: dict[str, Any]) -> bool:
        """Only publisher reconnects may use the bounded positive cache.

        Relay reads are user-facing authorization decisions. A stale relay
        grant must never outlive a Control Plane outage merely because it was
        recently accepted.
        """
        return str(payload.get("action", "")) == CACHED_ACTION


def _json_response(
    status: int,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> ProxyResponse:
    return ProxyResponse(
        status=status,
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers or {},
    )
