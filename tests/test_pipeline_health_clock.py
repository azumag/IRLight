from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from pipeline_health import apply_pipeline_health  # noqa: E402


class PipelineHealthClockValidationTest(unittest.TestCase):
    def _assert_clock_rejected(self, value: object) -> None:
        store = Mock()
        node = {"desired_state": "RUNNING"}
        before = dict(node)

        with self.assertRaisesRegex(
            ValueError,
            "pipeline health observed_at must be a finite non-negative number",
        ):
            apply_pipeline_health(
                store,
                node=node,
                node_id="node-clock",
                session_id="session-clock",
                node_status="STOPPING",
                media_health="stopped",
                observed_at=value,  # type: ignore[arg-type]
                grace_seconds=0.0,
            )

        self.assertEqual(node, before)
        store.get.assert_not_called()

    def test_explicit_invalid_clock_fails_before_node_or_session_mutation(self) -> None:
        invalid_values = (
            ("boolean", True),
            ("negative", -1.0),
            ("nan", float("nan")),
            ("positive-infinity", float("inf")),
            ("negative-infinity", float("-inf")),
            ("float-overflow", 10**400),
        )
        for label, value in invalid_values:
            with self.subTest(case=label):
                self._assert_clock_rejected(value)

    def test_invalid_system_clock_uses_same_fail_closed_boundary(self) -> None:
        store = Mock()
        node = {"desired_state": "RUNNING"}
        before = dict(node)

        with patch("pipeline_health.time.time", return_value=float("nan")):
            with self.assertRaisesRegex(
                ValueError,
                "pipeline health observed_at must be a finite non-negative number",
            ):
                apply_pipeline_health(
                    store,
                    node=node,
                    node_id="node-clock",
                    session_id="session-clock",
                    node_status="STOPPING",
                    media_health="stopped",
                    grace_seconds=0.0,
                )

        self.assertEqual(node, before)
        store.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
