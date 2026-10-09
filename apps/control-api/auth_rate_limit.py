"""Shared rate limiting for authentication attempts.

``POST /v1/auth/register`` and ``POST /v1/auth/login`` run deliberately
expensive password verification.  The KDF admission gate in
``auth_kdf_admission`` bounds how much of that work runs *at once*; this module
bounds how many attempts a single client/account pair may make *over time*, so
one caller cannot keep every KDF slot busy by itself.

Two fixed windows are enforced per key: a short burst window and a longer
sustained window.  An attempt is admitted only when both windows still have
room.  When a window is exhausted the caller receives a retry delay, and the
key recovers on its own once the longest window elapses, so no operator action
and no manual unlock is needed: a third party cannot lock a legitimate account
out permanently.

The counters are shared by every worker process that uses the same runtime
filesystem.  Each shard is a small JSON file guarded by ``flock``, so a
multi-worker Control Plane enforces one limit instead of multiplying the limit
by the worker count.  The persisted key is an HMAC of the client address and
the normalized email under a random secret, so the state never contains a raw
address, email, password, user ID, session token, or CSRF token.

Two failure modes are deliberately separated:

* An unsafe or unusable admission *boundary* (symlinked directory, foreign
  owner, world-writable directory or shard, unreadable secret, damaged clock)
  fails closed, because that boundary is what keeps counters from being forged
  or erased.
* Damaged counter *content* only loses the window it stored: the shard is
  treated as empty.  These counters are ephemeral admission state, not
  authority, so a damaged runtime must not turn into a total authentication
  outage.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BURST_LIMIT = 10
DEFAULT_BURST_WINDOW_SECONDS = 60.0
DEFAULT_SUSTAINED_LIMIT = 30
DEFAULT_SUSTAINED_WINDOW_SECONDS = 600.0
MAX_ATTEMPT_LIMIT = 10_000
MIN_WINDOW_SECONDS = 1.0
MAX_WINDOW_SECONDS = 24 * 60 * 60.0
DEFAULT_SHARD_COUNT = 16
MAX_SHARD_COUNT = 256
DEFAULT_MAX_TRACKED_KEYS_PER_SHARD = 4096
MAX_TRACKED_KEYS_PER_SHARD_LIMIT = 65_536
DEFAULT_ADMISSION_DIR = "/tmp/irlight-auth-rate-limit"
SECRET_FILENAME = "rate-limit.secret"
SECRET_BYTES = 32
SECRET_HEX_LENGTH = SECRET_BYTES * 2
SECRET_READ_ATTEMPTS = 50
SECRET_READ_DELAY_SECONDS = 0.005
CREATE_RETRY_ATTEMPTS = 25
CREATE_RETRY_DELAY_SECONDS = 0.002
MAX_STATE_FILE_BYTES = 4 * 1024 * 1024
STATE_READ_CHUNK_BYTES = 65_536
ATTEMPT_KEY_CHARS = 32
WINDOW_COUNT = 2


class AuthRateLimitExceeded(RuntimeError):
    """A window for this address/account pair is exhausted."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("authentication attempt rate is limited")
        self.retry_after_seconds = int(retry_after_seconds)


class AuthRateLimitUnavailable(RuntimeError):
    """The shared authentication admission state cannot be used safely."""


def _validated_limit(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > MAX_ATTEMPT_LIMIT
    ):
        raise ValueError(f"authentication {name} limit must be between 1 and {MAX_ATTEMPT_LIMIT}")
    return value


def _validated_window(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"authentication {name} window must be a finite number of seconds")
    window = float(value)
    if not math.isfinite(window) or window < MIN_WINDOW_SECONDS or window > MAX_WINDOW_SECONDS:
        raise ValueError(
            f"authentication {name} window must be between {MIN_WINDOW_SECONDS} "
            f"and {MAX_WINDOW_SECONDS} seconds"
        )
    return window


def _validated_bounded_int(value: object, minimum: int, maximum: int, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ValueError(f"authentication {name} must be between {minimum} and {maximum}")
    return value


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(os.getenv(name, str(default)), 10)
    except (TypeError, ValueError):
        return default
    if parsed < minimum or parsed > maximum:
        return default
    return parsed


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        return default
    return parsed


def _env_directory(name: str, default: str) -> Path:
    candidate = Path(os.getenv(name, default))
    if not candidate.is_absolute():
        candidate = Path(default)
    return candidate


@dataclass(frozen=True)
class AuthRateLimitConfig:
    burst_limit: int = DEFAULT_BURST_LIMIT
    burst_window_seconds: float = DEFAULT_BURST_WINDOW_SECONDS
    sustained_limit: int = DEFAULT_SUSTAINED_LIMIT
    sustained_window_seconds: float = DEFAULT_SUSTAINED_WINDOW_SECONDS
    shard_count: int = DEFAULT_SHARD_COUNT
    max_tracked_keys_per_shard: int = DEFAULT_MAX_TRACKED_KEYS_PER_SHARD
    directory: Path = Path(DEFAULT_ADMISSION_DIR)

    def __post_init__(self) -> None:
        _validated_limit(self.burst_limit, "burst")
        _validated_limit(self.sustained_limit, "sustained")
        _validated_window(self.burst_window_seconds, "burst")
        _validated_window(self.sustained_window_seconds, "sustained")
        _validated_bounded_int(self.shard_count, 1, MAX_SHARD_COUNT, "shard count")
        _validated_bounded_int(
            self.max_tracked_keys_per_shard,
            1,
            MAX_TRACKED_KEYS_PER_SHARD_LIMIT,
            "tracked key bound",
        )
        if not isinstance(self.directory, Path) or not self.directory.is_absolute():
            raise ValueError("authentication rate limit directory must be an absolute path")

    @property
    def windows(self) -> tuple[tuple[int, float], ...]:
        return (
            (int(self.burst_limit), float(self.burst_window_seconds)),
            (int(self.sustained_limit), float(self.sustained_window_seconds)),
        )

    @classmethod
    def from_env(cls) -> "AuthRateLimitConfig":
        return cls(
            burst_limit=_env_int(
                "IRLIGHT_AUTH_RATE_LIMIT_BURST_LIMIT", DEFAULT_BURST_LIMIT, 1, MAX_ATTEMPT_LIMIT
            ),
            burst_window_seconds=_env_float(
                "IRLIGHT_AUTH_RATE_LIMIT_BURST_WINDOW_SECONDS",
                DEFAULT_BURST_WINDOW_SECONDS,
                MIN_WINDOW_SECONDS,
                MAX_WINDOW_SECONDS,
            ),
            sustained_limit=_env_int(
                "IRLIGHT_AUTH_RATE_LIMIT_SUSTAINED_LIMIT",
                DEFAULT_SUSTAINED_LIMIT,
                1,
                MAX_ATTEMPT_LIMIT,
            ),
            sustained_window_seconds=_env_float(
                "IRLIGHT_AUTH_RATE_LIMIT_SUSTAINED_WINDOW_SECONDS",
                DEFAULT_SUSTAINED_WINDOW_SECONDS,
                MIN_WINDOW_SECONDS,
                MAX_WINDOW_SECONDS,
            ),
            directory=_env_directory("IRLIGHT_AUTH_RATE_LIMIT_DIR", DEFAULT_ADMISSION_DIR),
        )


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _safe_directory(stat_result: os.stat_result) -> bool:
    return (
        not stat.S_ISLNK(stat_result.st_mode)
        and stat.S_ISDIR(stat_result.st_mode)
        and stat_result.st_uid == os.geteuid()
        and not stat_result.st_mode & 0o077
    )


def _safe_file(stat_result: os.stat_result) -> bool:
    return (
        stat.S_ISREG(stat_result.st_mode)
        and stat_result.st_uid == os.geteuid()
        and not stat_result.st_mode & 0o077
    )


def _open_directory(path: Path) -> int:
    """Create, validate, and pin the configured admission directory."""

    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path_stat = path.lstat()
    except OSError as exc:
        raise AuthRateLimitUnavailable("authentication rate limit is unavailable") from exc
    if not _safe_directory(path_stat):
        raise AuthRateLimitUnavailable("authentication rate limit is unavailable")

    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd: int | None = None
    try:
        fd = os.open(path, flags)
        opened_stat = os.fstat(fd)
        if not _safe_directory(opened_stat) or not _same_file(path_stat, opened_stat):
            raise AuthRateLimitUnavailable("authentication rate limit is unavailable")
        return fd
    except AuthRateLimitUnavailable:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise AuthRateLimitUnavailable("authentication rate limit is unavailable") from exc


def _create_in_directory(name: str, flags: int, mode: int, directory_fd: int) -> int:
    """Open (creating when requested) an entry inside the pinned directory.

    ``openat`` with ``O_CREAT`` can transiently report ``ENOENT`` while several
    workers create entries in the same directory at the same time, even though
    the pinned directory is valid and writable. The retry below is bounded and
    only covers that transient case: any other error, and a persistent
    ``ENOENT``, still fails closed in the caller.
    """

    attempts = max(1, CREATE_RETRY_ATTEMPTS)
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            return os.open(name, flags, mode, dir_fd=directory_fd)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(CREATE_RETRY_DELAY_SECONDS)
    assert last_error is not None
    raise last_error


def _open_shard(directory_fd: int, name: str) -> int:
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd: int | None = None
    try:
        fd = _create_in_directory(name, flags, 0o600, directory_fd)
        if not _safe_file(os.fstat(fd)):
            raise AuthRateLimitUnavailable("authentication rate limit is unavailable")
        # A shard is opened by path, so re-check that the pinned directory and
        # the opened shard still are what the validated boundary promised.
        directory_stat = os.fstat(directory_fd)
        path_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not _safe_directory(directory_stat)
            or not _safe_file(path_stat)
            or not _same_file(os.fstat(fd), path_stat)
        ):
            raise AuthRateLimitUnavailable("authentication rate limit is unavailable")
        return fd
    except AuthRateLimitUnavailable:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise AuthRateLimitUnavailable("authentication rate limit is unavailable") from exc


def _read_secret_file(directory_fd: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(SECRET_FILENAME, flags, dir_fd=directory_fd)
    try:
        if not _safe_file(os.fstat(fd)):
            raise AuthRateLimitUnavailable("authentication rate limit is unavailable")
        return os.read(fd, SECRET_HEX_LENGTH + 1)
    finally:
        os.close(fd)


def _load_secret(directory_fd: int) -> bytes:
    """Return the shared HMAC secret, creating it owner-only when absent."""

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = _create_in_directory(SECRET_FILENAME, flags, 0o600, directory_fd)
    except FileExistsError:
        fd = None
    except OSError as exc:
        raise AuthRateLimitUnavailable("authentication rate limit is unavailable") from exc

    if fd is not None:
        try:
            if not _safe_file(os.fstat(fd)):
                raise AuthRateLimitUnavailable("authentication rate limit is unavailable")
            token = secrets.token_hex(SECRET_BYTES).encode("ascii")
            written = 0
            while written < len(token):
                written += os.write(fd, token[written:])
            os.fsync(fd)
            return bytes.fromhex(token.decode("ascii"))
        except AuthRateLimitUnavailable:
            raise
        except (OSError, ValueError) as exc:
            raise AuthRateLimitUnavailable("authentication rate limit is unavailable") from exc
        finally:
            os.close(fd)

    # Another worker is creating the secret right now. Its write is a single
    # short append, so a few bounded retries see the completed file.
    for attempt in range(SECRET_READ_ATTEMPTS):
        try:
            raw = _read_secret_file(directory_fd)
        except FileNotFoundError:
            raw = b""
        except OSError as exc:
            raise AuthRateLimitUnavailable("authentication rate limit is unavailable") from exc
        if len(raw) == SECRET_HEX_LENGTH:
            try:
                return bytes.fromhex(raw.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                raise AuthRateLimitUnavailable(
                    "authentication rate limit is unavailable"
                ) from None
        if attempt + 1 < SECRET_READ_ATTEMPTS:
            time.sleep(SECRET_READ_DELAY_SECONDS)
    raise AuthRateLimitUnavailable("authentication rate limit is unavailable")


def _normalize_email(email: object) -> str:
    if not isinstance(email, str):
        return ""
    return email.strip().lower()


def _attempt_key(secret: bytes, address: str | None, email: object) -> str:
    material = f"{address or '-'}\x00{_normalize_email(email)}".encode("utf-8")
    return hmac.new(secret, material, hashlib.sha256).hexdigest()[:ATTEMPT_KEY_CHARS]


def _shard_name(key: str, shard_count: int) -> str:
    return f"shard-{int(key[:8], 16) % shard_count}.json"


def _validated_now(now: object) -> float:
    candidate = time.time() if now is None else now
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise AuthRateLimitUnavailable("authentication rate limit clock is invalid")
    try:
        current = float(candidate)
    except (OverflowError, ValueError):
        raise AuthRateLimitUnavailable("authentication rate limit clock is invalid") from None
    if not math.isfinite(current) or current < 0:
        raise AuthRateLimitUnavailable("authentication rate limit clock is invalid")
    return current


def _validated_window_record(record: object) -> dict[str, float] | None:
    if not isinstance(record, dict):
        return None
    reset_at = record.get("reset_at")
    count = record.get("count")
    if isinstance(reset_at, bool) or not isinstance(reset_at, (int, float)):
        return None
    try:
        normalized_reset = float(reset_at)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(normalized_reset) or normalized_reset < 0:
        return None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return {"reset_at": normalized_reset, "count": count}


def _validated_state(payload: object) -> dict[str, list[dict[str, float]]]:
    if not isinstance(payload, dict):
        return {}
    keys = payload.get("keys")
    if not isinstance(keys, dict):
        return {}
    validated: dict[str, list[dict[str, float]]] = {}
    for key, windows in keys.items():
        if not isinstance(key, str) or not key or len(key) > ATTEMPT_KEY_CHARS:
            return {}
        if not isinstance(windows, list) or len(windows) != WINDOW_COUNT:
            return {}
        records = []
        for window in windows:
            record = _validated_window_record(window)
            if record is None:
                return {}
            records.append(record)
        validated[key] = records
    return validated


def _read_shard(fd: int) -> dict[str, list[dict[str, float]]]:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, STATE_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_STATE_FILE_BYTES:
            return {}
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return _validated_state(payload)


def _write_shard(fd: int, keys: dict[str, list[dict[str, float]]]) -> None:
    try:
        data = json.dumps(
            {"keys": keys},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuthRateLimitUnavailable("authentication rate limit is unavailable") from exc
    os.lseek(fd, 0, os.SEEK_SET)
    written = 0
    while written < len(data):
        written += os.write(fd, data[written:])
    os.ftruncate(fd, len(data))
    os.fsync(fd)


def _purge_expired(keys: dict[str, list[dict[str, float]]], now: float) -> None:
    for stored_key in list(keys):
        if all(record["reset_at"] <= now for record in keys[stored_key]):
            del keys[stored_key]


def _earliest_release(keys: dict[str, list[dict[str, float]]], now: float) -> int:
    earliest = min(
        max(record["reset_at"] for record in windows) for windows in keys.values()
    )
    return max(1, math.ceil(earliest - now))


def _register_attempt(
    keys: dict[str, list[dict[str, float]]],
    key: str,
    now: float,
    config: AuthRateLimitConfig,
) -> tuple[bool, int, bool]:
    """Charge one attempt and report whether the stored state changed."""

    changed = False
    tracked = len(keys)
    _purge_expired(keys, now)
    if len(keys) != tracked:
        changed = True

    windows = keys.get(key)
    if windows is None:
        if len(keys) >= config.max_tracked_keys_per_shard:
            # The table is bounded on purpose: instead of growing without limit,
            # report when a slot frees up. The delay is transient like any other
            # window, so saturation cannot become a permanent lockout.
            return False, _earliest_release(keys, now), changed
        windows = [{"reset_at": now, "count": 0} for _ in range(WINDOW_COUNT)]
        keys[key] = windows
        changed = True

    blocked_until: list[float] = []
    for index, (limit, window_seconds) in enumerate(config.windows):
        record = windows[index]
        if record["reset_at"] <= now:
            record = {"reset_at": now + window_seconds, "count": 0}
            windows[index] = record
            changed = True
        if record["count"] >= limit:
            blocked_until.append(record["reset_at"])
    if blocked_until:
        return False, max(1, math.ceil(max(blocked_until) - now)), changed

    for record in windows:
        record["count"] += 1
    return True, 0, True


def enforce_authentication_rate_limit(
    client_address: str | None,
    email: object,
    *,
    config: AuthRateLimitConfig | None = None,
    now: object = None,
) -> None:
    """Charge one authentication attempt for this address/email pair.

    Raises :class:`AuthRateLimitExceeded` when a window is exhausted and
    :class:`AuthRateLimitUnavailable` when the shared admission boundary cannot
    be used safely.
    """

    cfg = config or AuthRateLimitConfig.from_env()
    current = _validated_now(now)

    directory_fd = _open_directory(cfg.directory)
    try:
        secret = _load_secret(directory_fd)
        key = _attempt_key(secret, client_address, email)
        shard_name = _shard_name(key, cfg.shard_count)
        shard_fd = _open_shard(directory_fd, shard_name)
        try:
            fcntl.flock(shard_fd, fcntl.LOCK_EX)
            try:
                keys = _read_shard(shard_fd)
                allowed, retry_after, changed = _register_attempt(
                    keys, key, current, cfg
                )
                # A denied attempt that changed nothing must not add disk writes
                # under a flood; the next admitted attempt persists the state.
                if changed:
                    _write_shard(shard_fd, keys)
            finally:
                fcntl.flock(shard_fd, fcntl.LOCK_UN)
        finally:
            os.close(shard_fd)
    finally:
        os.close(directory_fd)

    if not allowed:
        raise AuthRateLimitExceeded(retry_after)
