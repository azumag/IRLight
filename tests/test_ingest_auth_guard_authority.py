from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from ingest_auth_guard import (  # noqa: E402
    IngestAuthGuard,
    IngestAuthGuardConfig,
    IngestAuthGuardStateError,
)


class IngestAuthGuardAuthorityTest(unittest.TestCase):
    def test_invalid_runtime_timestamp_is_controlled_and_does_not_replace_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard = IngestAuthGuard(tmp, config=IngestAuthGuardConfig())
            guard.record_failure(
                source_ip="203.0.113.10",
                username="candidate-user",
                protocol="rtmp",
                now=10.0,
            )
            baseline = guard.path.read_bytes()

            operations = (
                "check",
                "record_failure",
                "record_blocked",
                "record_success",
            )
            invalid_values = (
                float("nan"),
                float("inf"),
                float("-inf"),
                -1.0,
                True,
                10**400,
            )
            for operation in operations:
                for invalid_now in invalid_values:
                    with self.subTest(operation=operation, now=repr(invalid_now)):
                        with self.assertRaises(IngestAuthGuardStateError):
                            if operation == "check":
                                guard.check(
                                    source_ip="203.0.113.10",
                                    username="candidate-user",
                                    now=invalid_now,
                                )
                            elif operation == "record_failure":
                                guard.record_failure(
                                    source_ip="203.0.113.10",
                                    username="candidate-user",
                                    protocol="rtmp",
                                    now=invalid_now,
                                )
                            elif operation == "record_blocked":
                                guard.record_blocked(
                                    source_ip="203.0.113.10",
                                    username="candidate-user",
                                    protocol="rtmp",
                                    now=invalid_now,
                                )
                            else:
                                guard.record_success(
                                    username="candidate-user",
                                    now=invalid_now,
                                )
                        self.assertEqual(guard.path.read_bytes(), baseline)

            restored = IngestAuthGuard(tmp, config=IngestAuthGuardConfig()).snapshot()
            self.assertGreaterEqual(restored["next_sequence"], 2)
            self.assertTrue(restored["events"])

    def test_persist_revalidates_in_memory_state_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard = IngestAuthGuard(tmp, config=IngestAuthGuardConfig())
            guard.record_failure(
                source_ip="203.0.113.11",
                username="candidate-user",
                protocol="rtmp",
                now=20.0,
            )
            baseline = guard.path.read_bytes()

            bucket = next(iter(guard._buckets.values()))
            bucket["last_seen_at"] = 10**400

            with self.assertRaises(IngestAuthGuardStateError):
                guard._persist()
            self.assertEqual(guard.path.read_bytes(), baseline)

            restored = IngestAuthGuard(tmp, config=IngestAuthGuardConfig()).snapshot()
            self.assertTrue(restored["buckets"])
            for restored_bucket in restored["buckets"].values():
                self.assertLess(restored_bucket["last_seen_at"], 10**100)


if __name__ == "__main__":
    unittest.main()
