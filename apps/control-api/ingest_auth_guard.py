"""Rate limiting and lockout state for MediaMTX ingest authentication."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import math
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class IngestAuthGuardConfig:
    enabled: bool = True
    failure_window_seconds: float = 60.0
    max_failures_per_ip: int = 20
    max_failures_per_credential: int = 8
    lockout_seconds: float = 120.0
    event_limit: int = 200
    bucket_limit: int = 4096
    blocked_event_interval_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> "IngestAuthGuardConfig":
        return cls(
            enabled=os.getenv("IRLIGHT_INGEST_AUTH_GUARD_ENABLED", "1") != "0",
            failure_window_seconds=_env_float(
                "IRLIGHT_INGEST_AUTH_FAILURE_WINDOW_SECONDS", 60.0, 1.0, 3600.0
            ),
            max_failures_per_ip=_env_int(
                "IRLIGHT_INGEST_AUTH_MAX_FAILURES_PER_IP", 20, 1, 10000
            ),
            max_failures_per_credential=_env_int(
                "IRLIGHT_INGEST_AUTH_MAX_FAILURES_PER_CREDENTIAL", 8, 1, 10000
            ),
            lockout_seconds=_env_float(
                "IRLIGHT_INGEST_AUTH_LOCKOUT_SECONDS", 120.0, 1.0, 86400.0
            ),
            event_limit=_env_int("IRLIGHT_INGEST_AUTH_EVENT_LIMIT", 200, 1, 5000),
            bucket_limit=_env_int("IRLIGHT_INGEST_AUTH_BUCKET_LIMIT", 4096, 32, 100000),
            blocked_event_interval_seconds=_env_float(
                "IRLIGHT_INGEST_AUTH_BLOCKED_EVENT_INTERVAL_SECONDS", 5.0, 0.1, 3600.0
            ),
        )


@dataclass(frozen=True)
class AuthGuardDecision:
    blocked: bool
    locked_scopes: tuple[str, ...] = ()
    retry_after_seconds: int = 0


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


def _safe_text(value: str, limit: int) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return normalized[:limit]


def _normalize_ip(value: str) -> str | None:
    raw = _safe_text(value, 128)
    if raw is None:
        return None
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return raw


def _session_id(value: str) -> str | None:
    raw = _safe_text(value, 128)
    if raw is None:
        return None
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return None


def _fingerprint(value: str) -> str | None:
    raw = _safe_text(value, 256)
    if raw is None:
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class IngestAuthGuard:
    """Persistent failure windows and temporary lockouts for ingest auth.

    Bucket keys are SHA-256 digests rather than attacker-controlled usernames or
    addresses. The state file intentionally never receives publisher passwords,
    tokens, query strings or user agents.
    """

    def __init__(
        self,
        state_dir: str | os.PathLike[str] | None = None,
        *,
        config: IngestAuthGuardConfig | None = None,
    ) -> None:
        self.state_dir = Path(state_dir or os.getenv("STATE_DIR", "/state"))
        self.path = self.state_dir / "ingest_auth_guard.json"
        self.config = config or IngestAuthGuardConfig.from_env()
        self.lock = threading.Lock()
        self._buckets: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._next_sequence = 1
        self._load()

    @staticmethod
    def _bucket_key(scope: str, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"{scope}:{digest}"

    def _scope_specs(self, source_ip: str, username: str) -> list[tuple[str, str, int]]:
        specs: list[tuple[str, str, int]] = []
        normalized_ip = _normalize_ip(source_ip)
        normalized_username = _safe_text(username, 256)
        if normalized_ip is not None:
            specs.append(("ip", normalized_ip, self.config.max_failures_per_ip))
        if normalized_username is not None:
            specs.append(
                ("credential", normalized_username, self.config.max_failures_per_credential)
            )
        return specs

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                return
            buckets = raw.get("buckets", {})
            events = raw.get("events", [])
            self._buckets = {
                str(key): value
                for key, value in buckets.items()
                if isinstance(key, str) and isinstance(value, dict)
            } if isinstance(buckets, dict) else {}
            self._events = [event for event in events if isinstance(event, dict)][
                -self.config.event_limit :
            ] if isinstance(events, list) else []
            try:
                self._next_sequence = max(1, int(raw.get("next_sequence", 1)))
            except (TypeError, ValueError):
                self._next_sequence = 1
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._buckets = {}
            self._events = []
            self._next_sequence = 1

    def _persist(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "buckets": self._buckets,
                        "events": self._events[-self.config.event_limit :],
                        "next_sequence": self._next_sequence,
                    },
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _prune_bucket(self, bucket: dict[str, Any], now: float) -> None:
        try:
            locked_until = float(bucket.get("locked_until", 0.0) or 0.0)
        except (TypeError, ValueError):
            locked_until = 0.0
        lock_expired = locked_until > 0.0 and locked_until <= now
        if lock_expired:
            locked_until = 0.0

        cutoff = now - self.config.failure_window_seconds
        failures: list[float] = []
        if not lock_expired:
            raw_failures = bucket.get("failures", [])
            if isinstance(raw_failures, list):
                for value in raw_failures:
                    try:
                        timestamp = float(value)
                    except (TypeError, ValueError):
                        continue
                    if cutoff <= timestamp <= now + 1.0:
                        failures.append(timestamp)
        bucket["failures"] = failures
        bucket["locked_until"] = locked_until

    def _compact_buckets(self, now: float) -> None:
        stale: list[str] = []
        for key, bucket in self._buckets.items():
            self._prune_bucket(bucket, now)
            if not bucket.get("failures") and not float(bucket.get("locked_until", 0.0) or 0.0):
                stale.append(key)
        for key in stale:
            self._buckets.pop(key, None)

        overflow = len(self._buckets) - self.config.bucket_limit
        if overflow > 0:
            oldest = sorted(
                self._buckets.items(),
                key=lambda item: float(item[1].get("last_seen_at", 0.0) or 0.0),
            )
            for key, _bucket in oldest[:overflow]:
                self._buckets.pop(key, None)

    def _decision(self, specs: list[tuple[str, str, int]], now: float) -> AuthGuardDecision:
        locked_scopes: list[str] = []
        retry_after = 0
        for scope, value, _limit in specs:
            bucket = self._buckets.get(self._bucket_key(scope, value))
            if bucket is None:
                continue
            self._prune_bucket(bucket, now)
            locked_until = float(bucket.get("locked_until", 0.0) or 0.0)
            if locked_until > now:
                locked_scopes.append(scope)
                retry_after = max(retry_after, max(1, math.ceil(locked_until - now)))
        return AuthGuardDecision(bool(locked_scopes), tuple(locked_scopes), retry_after)

    def _event_payload(
        self,
        *,
        source_ip: str,
        username: str,
        protocol: str,
        publisher_id: str,
        decision: AuthGuardDecision,
    ) -> dict[str, Any]:
        return {
            "session_id": _session_id(username),
            "credential_fingerprint": _fingerprint(username),
            "source_ip": _normalize_ip(source_ip),
            "protocol": _safe_text(protocol.lower(), 32),
            "publisher_id": _safe_text(publisher_id, 128),
            "locked_scopes": list(decision.locked_scopes),
            "retry_after_seconds": decision.retry_after_seconds,
        }

    def _append_event(self, event_type: str, occurred_at: float, payload: dict[str, Any]) -> None:
        self._events.append(
            {
                "sequence": self._next_sequence,
                "type": event_type,
                "occurred_at": occurred_at,
                "payload": payload,
            }
        )
        self._next_sequence += 1
        self._events = self._events[-self.config.event_limit :]

    def check(
        self,
        *,
        source_ip: str,
        username: str,
        now: float | None = None,
    ) -> AuthGuardDecision:
        if not self.config.enabled:
            return AuthGuardDecision(False)
        current = time.time() if now is None else now
        specs = self._scope_specs(source_ip, username)
        with self.lock:
            self._compact_buckets(current)
            return self._decision(specs, current)

    def record_failure(
        self,
        *,
        source_ip: str,
        username: str,
        protocol: str = "",
        publisher_id: str = "",
        now: float | None = None,
    ) -> AuthGuardDecision:
        if not self.config.enabled:
            return AuthGuardDecision(False)
        current = time.time() if now is None else now
        specs = self._scope_specs(source_ip, username)
        with self.lock:
            self._compact_buckets(current)
            newly_locked: set[str] = set()
            for scope, value, limit in specs:
                key = self._bucket_key(scope, value)
                bucket = self._buckets.setdefault(
                    key,
                    {"failures": [], "locked_until": 0.0, "last_seen_at": current},
                )
                self._prune_bucket(bucket, current)
                was_locked = float(bucket.get("locked_until", 0.0) or 0.0) > current
                failures = list(bucket.get("failures", []))
                failures.append(current)
                bucket["failures"] = failures
                bucket["last_seen_at"] = current
                if not was_locked and len(failures) >= limit:
                    bucket["locked_until"] = current + self.config.lockout_seconds
                    newly_locked.add(scope)

            self._compact_buckets(current)
            decision = self._decision(specs, current)
            payload = self._event_payload(
                source_ip=source_ip,
                username=username,
                protocol=protocol,
                publisher_id=publisher_id,
                decision=decision,
            )
            self._append_event("ingest.auth_failed", current, payload)
            if newly_locked:
                self._append_event(
                    "ingest.auth_locked",
                    current,
                    {**payload, "newly_locked_scopes": sorted(newly_locked)},
                )
            self._persist()
            return decision

    def record_blocked(
        self,
        *,
        source_ip: str,
        username: str,
        protocol: str = "",
        publisher_id: str = "",
        now: float | None = None,
    ) -> AuthGuardDecision:
        if not self.config.enabled:
            return AuthGuardDecision(False)
        current = time.time() if now is None else now
        specs = self._scope_specs(source_ip, username)
        with self.lock:
            self._compact_buckets(current)
            decision = self._decision(specs, current)
            if not decision.blocked:
                return decision

            locked_scope_set = set(decision.locked_scopes)
            due_buckets: list[dict[str, Any]] = []
            for scope, value, _limit in specs:
                if scope not in locked_scope_set:
                    continue
                bucket = self._buckets.get(self._bucket_key(scope, value))
                if bucket is None:
                    continue
                try:
                    last_event = float(bucket.get("last_blocked_event_at", 0.0) or 0.0)
                except (TypeError, ValueError):
                    last_event = 0.0
                if current - last_event >= self.config.blocked_event_interval_seconds:
                    due_buckets.append(bucket)

            if due_buckets:
                for bucket in due_buckets:
                    bucket["last_blocked_event_at"] = current
                self._append_event(
                    "ingest.auth_blocked",
                    current,
                    self._event_payload(
                        source_ip=source_ip,
                        username=username,
                        protocol=protocol,
                        publisher_id=publisher_id,
                        decision=decision,
                    ),
                )
                self._persist()
            return decision

    def record_success(
        self,
        *,
        username: str,
        now: float | None = None,
    ) -> None:
        if not self.config.enabled:
            return
        normalized = _safe_text(username, 256)
        if normalized is None:
            return
        current = time.time() if now is None else now
        key = self._bucket_key("credential", normalized)
        with self.lock:
            changed = self._buckets.pop(key, None) is not None
            self._compact_buckets(current)
            if changed:
                self._persist()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(
                {
                    "buckets": self._buckets,
                    "events": self._events,
                    "next_sequence": self._next_sequence,
                }
            )


_DEFAULT_GUARD: IngestAuthGuard | None = None


def default_ingest_auth_guard() -> IngestAuthGuard:
    global _DEFAULT_GUARD
    if _DEFAULT_GUARD is None:
        _DEFAULT_GUARD = IngestAuthGuard()
    return _DEFAULT_GUARD
