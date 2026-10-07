from __future__ import annotations

import json
import multiprocessing
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from auth_rate_limit import (  # noqa: E402
    DEFAULT_ADMISSION_DIR,
    DEFAULT_BURST_LIMIT,
    DEFAULT_BURST_WINDOW_SECONDS,
    DEFAULT_SUSTAINED_LIMIT,
    DEFAULT_SUSTAINED_WINDOW_SECONDS,
    MAX_ATTEMPT_LIMIT,
    SECRET_FILENAME,
    AuthRateLimitConfig,
    AuthRateLimitExceeded,
    AuthRateLimitUnavailable,
    enforce_authentication_rate_limit,
)


def _config(temp_dir: str, **overrides) -> AuthRateLimitConfig:
    options = {
        "burst_limit": 2,
        "burst_window_seconds": 60.0,
        "sustained_limit": 2,
        "sustained_window_seconds": 600.0,
        "directory": Path(temp_dir),
    }
    options.update(overrides)
    return AuthRateLimitConfig(**options)


def _attempt(
    config: AuthRateLimitConfig,
    *,
    address: str | None = "192.0.2.10",
    email: str = "alice@example.com",
    now: object = 0.0,
) -> None:
    enforce_authentication_rate_limit(address, email, config=config, now=now)


def _shard_state(directory: str) -> dict:
    payload = {"keys": {}}
    for path in sorted(Path(directory).glob("shard-*.json")):
        payload["keys"].update(json.loads(path.read_text(encoding="utf-8"))["keys"])
    return payload


class _AttemptResult:
    ADMITTED = 1
    LIMITED = 0
    UNAVAILABLE = -1


def _concurrent_attempt(directory: str, limit: int, start, results) -> None:
    config = AuthRateLimitConfig(
        burst_limit=limit,
        burst_window_seconds=60.0,
        sustained_limit=limit,
        sustained_window_seconds=60.0,
        directory=Path(directory),
    )
    start.wait(timeout=30)
    try:
        enforce_authentication_rate_limit("192.0.2.10", "alice@example.com", config=config)
    except AuthRateLimitExceeded:
        results.put(_AttemptResult.LIMITED)
    except AuthRateLimitUnavailable:
        results.put(_AttemptResult.UNAVAILABLE)
    else:
        results.put(_AttemptResult.ADMITTED)


class AuthRateLimitConfigTest(unittest.TestCase):
    def test_invalid_limits_are_rejected(self) -> None:
        for value in (0, -1, True, MAX_ATTEMPT_LIMIT + 1, "5", 2.5, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    AuthRateLimitConfig(burst_limit=value)

    def test_invalid_windows_are_rejected(self) -> None:
        for value in (0.0, 0.5, float("nan"), float("inf"), True, "60", 86_401.0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    AuthRateLimitConfig(sustained_window_seconds=value)

    def test_invalid_directory_and_bounds_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AuthRateLimitConfig(directory=Path("relative/dir"))
        with self.assertRaises(ValueError):
            AuthRateLimitConfig(shard_count=0)
        with self.assertRaises(ValueError):
            AuthRateLimitConfig(max_tracked_keys_per_shard=0)

    def test_environment_defaults_are_bounded(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            for name in (
                "IRLIGHT_AUTH_RATE_LIMIT_BURST_LIMIT",
                "IRLIGHT_AUTH_RATE_LIMIT_BURST_WINDOW_SECONDS",
                "IRLIGHT_AUTH_RATE_LIMIT_SUSTAINED_LIMIT",
                "IRLIGHT_AUTH_RATE_LIMIT_SUSTAINED_WINDOW_SECONDS",
                "IRLIGHT_AUTH_RATE_LIMIT_DIR",
            ):
                os.environ.pop(name, None)
            config = AuthRateLimitConfig.from_env()
        self.assertEqual(config.burst_limit, DEFAULT_BURST_LIMIT)
        self.assertEqual(config.burst_window_seconds, DEFAULT_BURST_WINDOW_SECONDS)
        self.assertEqual(config.sustained_limit, DEFAULT_SUSTAINED_LIMIT)
        self.assertEqual(config.sustained_window_seconds, DEFAULT_SUSTAINED_WINDOW_SECONDS)
        self.assertEqual(config.directory, Path(DEFAULT_ADMISSION_DIR))

    def test_invalid_environment_values_fall_back_to_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IRLIGHT_AUTH_RATE_LIMIT_BURST_LIMIT": "0",
                "IRLIGHT_AUTH_RATE_LIMIT_SUSTAINED_LIMIT": "not-a-number",
                "IRLIGHT_AUTH_RATE_LIMIT_BURST_WINDOW_SECONDS": "nan",
                "IRLIGHT_AUTH_RATE_LIMIT_SUSTAINED_WINDOW_SECONDS": "1000000",
                "IRLIGHT_AUTH_RATE_LIMIT_DIR": "relative/dir",
            },
            clear=False,
        ):
            config = AuthRateLimitConfig.from_env()
        self.assertEqual(config.burst_limit, DEFAULT_BURST_LIMIT)
        self.assertEqual(config.sustained_limit, DEFAULT_SUSTAINED_LIMIT)
        self.assertEqual(config.burst_window_seconds, DEFAULT_BURST_WINDOW_SECONDS)
        self.assertEqual(config.sustained_window_seconds, DEFAULT_SUSTAINED_WINDOW_SECONDS)
        self.assertEqual(config.directory, Path(DEFAULT_ADMISSION_DIR))

    def test_valid_environment_overrides_are_applied(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "IRLIGHT_AUTH_RATE_LIMIT_BURST_LIMIT": "3",
                    "IRLIGHT_AUTH_RATE_LIMIT_SUSTAINED_LIMIT": "9",
                    "IRLIGHT_AUTH_RATE_LIMIT_BURST_WINDOW_SECONDS": "30",
                    "IRLIGHT_AUTH_RATE_LIMIT_SUSTAINED_WINDOW_SECONDS": "300",
                    "IRLIGHT_AUTH_RATE_LIMIT_DIR": temp_dir,
                },
                clear=False,
            ):
                config = AuthRateLimitConfig.from_env()
        self.assertEqual(config.burst_limit, 3)
        self.assertEqual(config.sustained_limit, 9)
        self.assertEqual(config.burst_window_seconds, 30.0)
        self.assertEqual(config.sustained_window_seconds, 300.0)
        self.assertEqual(config.directory, Path(temp_dir))


class AuthRateLimitTest(unittest.TestCase):
    def test_attempts_are_admitted_up_to_the_burst_limit_then_recover(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            config = _config(temp_dir, burst_limit=2, sustained_limit=4)
            _attempt(config, now=0.0)
            _attempt(config, now=0.0)
            with self.assertRaises(AuthRateLimitExceeded) as raised:
                _attempt(config, now=1.0)
            self.assertEqual(raised.exception.retry_after_seconds, 59)
            # The window clears without any operator action.
            _attempt(config, now=61.0)

    def test_sustained_window_blocks_after_burst_windows_reset(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            config = _config(
                temp_dir,
                burst_limit=5,
                burst_window_seconds=10.0,
                sustained_limit=3,
                sustained_window_seconds=60.0,
            )
            for now in (0.0, 11.0, 22.0):
                _attempt(config, now=now)
            with self.assertRaises(AuthRateLimitExceeded) as raised:
                _attempt(config, now=33.0)
            self.assertEqual(raised.exception.retry_after_seconds, 27)
            _attempt(config, now=62.0)

    def test_retry_after_reports_the_window_that_is_still_blocked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            config = _config(
                temp_dir,
                burst_limit=1,
                burst_window_seconds=30.0,
                sustained_limit=1,
                sustained_window_seconds=300.0,
            )
            _attempt(config, now=0.0)
            with self.assertRaises(AuthRateLimitExceeded) as raised:
                _attempt(config, now=31.0)
            self.assertEqual(raised.exception.retry_after_seconds, 269)

    def test_distinct_addresses_and_emails_have_independent_limits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            config = _config(temp_dir, burst_limit=1, sustained_limit=1)
            _attempt(config, address="192.0.2.10", email="alice@example.com", now=0.0)
            _attempt(config, address="192.0.2.11", email="alice@example.com", now=0.0)
            _attempt(config, address="192.0.2.10", email="bob@example.com", now=0.0)
            # Email normalization matches the store contract: the padded,
            # differently cased address is the same key as the first attempt.
            with self.assertRaises(AuthRateLimitExceeded):
                _attempt(config, address="192.0.2.10", email=" ALICE@Example.COM ", now=1.0)
            with self.assertRaises(AuthRateLimitExceeded):
                _attempt(config, address="192.0.2.10", email="alice@example.com", now=1.0)

    def test_missing_address_is_keyed_on_a_shared_marker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            config = _config(temp_dir, burst_limit=1, sustained_limit=1)
            _attempt(config, address=None, now=0.0)
            with self.assertRaises(AuthRateLimitExceeded):
                _attempt(config, address=None, now=0.0)

    def test_state_contains_no_raw_identifiers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            config = _config(temp_dir)
            _attempt(config, address="203.0.113.9", email="alice@example.com", now=0.0)
            state = _shard_state(temp_dir)
            self.assertTrue(state["keys"])
            serialized = json.dumps(state)
            self.assertNotIn("alice@example.com", serialized)
            self.assertNotIn("203.0.113.9", serialized)
            self.assertNotIn("alice", serialized)

    def test_denied_attempts_do_not_grow_the_stored_counter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            config = _config(temp_dir, burst_limit=1, sustained_limit=1)
            _attempt(config, now=0.0)
            for _ in range(20):
                with self.assertRaises(AuthRateLimitExceeded):
                    _attempt(config, now=1.0)
            counts = [record["count"] for windows in _shard_state(temp_dir)["keys"].values() for record in windows]
            self.assertEqual(counts, [1, 1])

    def test_denied_attempt_does_not_rewrite_the_shard(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            config = _config(temp_dir, burst_limit=1, sustained_limit=1)
            _attempt(config, now=0.0)
            shard = next(Path(temp_dir).glob("shard-*.json"))
            before = shard.stat()
            for _ in range(5):
                with self.assertRaises(AuthRateLimitExceeded):
                    _attempt(config, now=1.0)
            after = shard.stat()
            # A flood of already-limited attempts must not keep fsyncing the
            # shard when the stored state did not change.
            self.assertEqual(
                (before.st_mtime_ns, before.st_size), (after.st_mtime_ns, after.st_size)
            )

    def test_shared_secret_is_created_owner_only_and_reused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            config = _config(temp_dir, burst_limit=2, sustained_limit=2)
            _attempt(config, now=0.0)
            secret_path = Path(temp_dir) / SECRET_FILENAME
            self.assertTrue(secret_path.is_file())
            self.assertFalse(secret_path.is_symlink())
            self.assertEqual(stat.S_IMODE(secret_path.stat().st_mode), 0o600)
            secret = secret_path.read_bytes()
            self.assertEqual(len(secret), 64)
            _attempt(config, now=1.0)
            self.assertEqual(secret_path.read_bytes(), secret)
            # The same key keeps counting, so the second attempt hit the limit.
            with self.assertRaises(AuthRateLimitExceeded):
                _attempt(config, now=2.0)

    def test_shard_files_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            config = _config(temp_dir)
            _attempt(config, now=0.0)
            shards = list(Path(temp_dir).glob("shard-*.json"))
            self.assertEqual(len(shards), 1)
            self.assertEqual(stat.S_IMODE(shards[0].stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(Path(temp_dir).stat().st_mode), 0o700)

    def test_group_or_world_accessible_directory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            os.chmod(temp_dir, 0o755)
            config = _config(temp_dir)
            with self.assertRaises(AuthRateLimitUnavailable):
                _attempt(config, now=0.0)

    def test_symlinked_shard_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            target = Path(temp_dir) / "outside"
            target.write_text("{}", encoding="utf-8")
            os.chmod(target, 0o600)
            config = _config(temp_dir)
            _attempt(config, now=0.0)
            shard = next(Path(temp_dir).glob("shard-*.json"))
            shard.unlink()
            shard.symlink_to(target)
            with self.assertRaises(AuthRateLimitUnavailable):
                _attempt(config, now=1.0)

    def test_damaged_shard_content_resets_the_window(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            config = _config(temp_dir, burst_limit=1, sustained_limit=1)
            _attempt(config, now=0.0)
            shard = next(Path(temp_dir).glob("shard-*.json"))
            shard.write_text("{not json", encoding="utf-8")
            # Ephemeral admission state, not authority: the window restarts
            # instead of turning a damaged runtime into an authentication outage.
            _attempt(config, now=1.0)
            with self.assertRaises(AuthRateLimitExceeded):
                _attempt(config, now=1.0)

    def test_unvalidated_shard_records_reset_the_window(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            config = _config(temp_dir, burst_limit=1, sustained_limit=1)
            _attempt(config, now=0.0)
            shard = next(Path(temp_dir).glob("shard-*.json"))
            shard.write_text(
                json.dumps({"keys": {"ab" * 16: [{"reset_at": float("inf"), "count": 0}]}}),
                encoding="utf-8",
            )
            _attempt(config, now=1.0)

    def test_invalid_clock_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            config = _config(temp_dir)
            for now in (float("nan"), float("inf"), float("-inf"), -1.0, True, "0", 10**400):
                with self.subTest(now=now):
                    with self.assertRaises(AuthRateLimitUnavailable):
                        _attempt(config, now=now)

    def test_bounded_table_reports_transient_recovery_instead_of_growing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            config = _config(
                temp_dir,
                burst_limit=5,
                burst_window_seconds=30.0,
                sustained_limit=5,
                sustained_window_seconds=60.0,
                shard_count=1,
                max_tracked_keys_per_shard=1,
            )
            _attempt(config, address="192.0.2.10", now=0.0)
            with self.assertRaises(AuthRateLimitExceeded) as raised:
                _attempt(config, address="192.0.2.11", now=1.0)
            self.assertGreaterEqual(raised.exception.retry_after_seconds, 1)
            self.assertEqual(len(_shard_state(temp_dir)["keys"]), 1)
            # Once the resident key expires, a new key is admitted again.
            _attempt(config, address="192.0.2.11", now=61.0)

    def test_concurrent_workers_share_one_counter(self) -> None:
        limit = 3
        workers = 8
        with tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-") as temp_dir:
            context = multiprocessing.get_context("fork")
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=_concurrent_attempt,
                    args=(temp_dir, limit, start, results),
                )
                for _ in range(workers)
            ]
            for process in processes:
                process.start()
            start.set()
            collected = [results.get(timeout=60) for _ in range(workers)]
            for process in processes:
                process.join(timeout=60)
                self.assertEqual(process.exitcode, 0)

        self.assertNotIn(_AttemptResult.UNAVAILABLE, collected)
        self.assertEqual(collected.count(_AttemptResult.ADMITTED), limit)
        self.assertEqual(collected.count(_AttemptResult.LIMITED), workers - limit)


if __name__ == "__main__":
    unittest.main()
