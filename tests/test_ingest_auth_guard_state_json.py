from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from ingest_auth_guard import (  # noqa: E402
    IngestAuthGuard,
    IngestAuthGuardConfig,
    IngestAuthGuardStateError,
)


def _config() -> IngestAuthGuardConfig:
    return IngestAuthGuardConfig(
        enabled=True,
        failure_window_seconds=60.0,
        max_failures_per_ip=20,
        max_failures_per_credential=8,
        lockout_seconds=120.0,
        event_limit=20,
        bucket_limit=64,
        blocked_event_interval_seconds=5.0,
    )


def _valid_state() -> dict[str, object]:
    digest = "0" * 64
    return {
        "buckets": {
            f"ip:{digest}": {
                "failures": [1.0],
                "locked_until": 0.0,
                "last_seen_at": 1.0,
            }
        },
        "events": [
            {
                "sequence": 1,
                "type": "ingest.auth_failed",
                "occurred_at": 1.0,
                "payload": {},
            }
        ],
        "next_sequence": 2,
    }


class IngestAuthGuardStateJsonTest(unittest.TestCase):
    def test_large_integer_timestamps_fail_as_controlled_state_errors(self) -> None:
        mutations = {
            "failure": lambda state, value: state["buckets"]["ip:" + "0" * 64][
                "failures"
            ].__setitem__(0, value),
            "bucket": lambda state, value: state["buckets"]["ip:" + "0" * 64].__setitem__(
                "locked_until", value
            ),
            "event": lambda state, value: state["events"][0].__setitem__(
                "occurred_at", value
            ),
        }
        huge = 10**1000

        for name, mutate in mutations.items():
            with self.subTest(field=name), tempfile.TemporaryDirectory() as tmp:
                state = copy.deepcopy(_valid_state())
                mutate(state, huge)
                Path(tmp, "ingest_auth_guard.json").write_text(
                    json.dumps(state), encoding="utf-8"
                )

                with self.assertRaises(IngestAuthGuardStateError):
                    IngestAuthGuard(tmp, config=_config())

    def test_non_finite_write_does_not_replace_last_readable_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard = IngestAuthGuard(tmp, config=_config())
            guard.record_failure(
                source_ip="203.0.113.10", username="candidate", now=1.0
            )
            before = guard.path.read_bytes()

            with self.assertRaises(IngestAuthGuardStateError):
                guard.record_failure(
                    source_ip="203.0.113.10",
                    username="candidate",
                    now=float("nan"),
                )

            self.assertEqual(guard.path.read_bytes(), before)
            snapshot = IngestAuthGuard(tmp, config=_config()).snapshot()
            self.assertEqual(snapshot["events"][-1]["occurred_at"], 1.0)

    def test_serializer_failure_does_not_replace_last_readable_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard = IngestAuthGuard(tmp, config=_config())
            guard.record_failure(
                source_ip="203.0.113.11", username="candidate", now=1.0
            )
            before = guard.path.read_bytes()

            with patch("ingest_auth_guard.json.dump", side_effect=TypeError("synthetic")):
                with self.assertRaises(IngestAuthGuardStateError):
                    guard.record_failure(
                        source_ip="203.0.113.11", username="candidate", now=2.0
                    )

            self.assertEqual(guard.path.read_bytes(), before)
            snapshot = IngestAuthGuard(tmp, config=_config()).snapshot()
            self.assertEqual(snapshot["events"][-1]["occurred_at"], 1.0)


if __name__ == "__main__":
    unittest.main()
