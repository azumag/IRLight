from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import node_internal  # noqa: E402
from node_internal import BootstrapRequest  # noqa: E402


class NodeBootstrapTransactionClockTest(unittest.TestCase):
    def _request(self) -> BootstrapRequest:
        return BootstrapRequest(
            provider_server_id="provider-clock-test",
            boot_id="boot-clock-test",
            agent_version="test",
            bootstrap_request_id="bootstrap-clock-request-1",
            node_access_token="node-clock-access-token-0123456789abcdef",
            public_address="198.51.100.10",
        )

    def _authority(self) -> dict[str, object]:
        return {"nodes": {}, "next_node_seq": 1, "tokens": {}}

    def test_invalid_bootstrap_clock_is_rejected_before_assignment_or_writes(self) -> None:
        invalid_values = (-1.0, math.nan, math.inf, -math.inf, 10**400)

        for value in invalid_values:
            with self.subTest(value=value):
                authority = self._authority()
                resolve = Mock()
                write_fuse = Mock()
                write_authority = Mock()
                with (
                    patch.object(node_internal, "_read_authority", return_value=authority),
                    patch.object(node_internal, "_resolve_assigned_session", resolve),
                    patch.object(node_internal, "_write_legacy_token_fuse", write_fuse),
                    patch.object(node_internal, "_write_authority", write_authority),
                    patch.object(node_internal.time, "time", return_value=value),
                ):
                    with self.assertRaises(node_internal.NodeStateError):
                        node_internal._bootstrap_locked(
                            self._request(), node_internal.hash_token("bootstrap-token")
                        )

                resolve.assert_not_called()
                write_fuse.assert_not_called()
                write_authority.assert_not_called()
                self.assertEqual(authority, self._authority())

    def test_deadline_overflow_is_rejected_before_assignment_or_writes(self) -> None:
        authority = self._authority()
        resolve = Mock()
        write_fuse = Mock()
        write_authority = Mock()
        with (
            patch.object(node_internal, "_read_authority", return_value=authority),
            patch.object(node_internal, "_resolve_assigned_session", resolve),
            patch.object(node_internal, "_write_legacy_token_fuse", write_fuse),
            patch.object(node_internal, "_write_authority", write_authority),
            patch.object(node_internal.time, "time", return_value=1.0),
            patch.dict("os.environ", {"NODE_ABSOLUTE_DEADLINE_HOURS": "1e308"}),
        ):
            with self.assertRaises(node_internal.NodeStateError):
                node_internal._bootstrap_locked(
                    self._request(), node_internal.hash_token("bootstrap-token")
                )

        resolve.assert_not_called()
        write_fuse.assert_not_called()
        write_authority.assert_not_called()
        self.assertEqual(authority, self._authority())

    def test_assigned_session_deadline_is_validated_before_bind_node(self) -> None:
        authority = self._authority()
        store = Mock()
        assigned = {
            "session_id": "session-clock-test",
            "absolute_deadline_at": math.inf,
        }
        with (
            patch.object(node_internal, "_read_authority", return_value=authority),
            patch.object(node_internal, "_resolve_assigned_session", return_value=assigned),
            patch.object(node_internal, "default_store", return_value=store),
            patch.object(node_internal, "_write_legacy_token_fuse") as write_fuse,
            patch.object(node_internal, "_write_authority") as write_authority,
            patch.object(node_internal.time, "time", return_value=10.0),
        ):
            with self.assertRaises(node_internal.NodeStateError):
                node_internal._bootstrap_locked(
                    self._request(), node_internal.hash_token("bootstrap-token")
                )

        store.bind_node.assert_not_called()
        write_fuse.assert_not_called()
        write_authority.assert_not_called()

    def test_bootstrap_response_reuses_one_validated_clock_sample(self) -> None:
        authority = self._authority()
        digest = node_internal.hash_token("bootstrap-token")
        clock = Mock(side_effect=[100.0, -1.0])

        with (
            patch.object(node_internal, "_read_authority", return_value=authority),
            patch.object(node_internal, "_resolve_assigned_session", return_value=None),
            patch.object(node_internal, "_write_legacy_token_fuse") as write_fuse,
            patch.object(node_internal, "_write_authority") as write_authority,
            patch.object(node_internal.time, "time", clock),
            patch.dict("os.environ", {"NODE_EGRESS_URL": ""}),
        ):
            response = node_internal._bootstrap_locked(self._request(), digest)

        clock.assert_called_once_with()
        write_authority.assert_called_once_with(authority)
        self.assertEqual(authority["next_node_seq"], 2)
        node = authority["nodes"]["node-0001"]
        token = authority["tokens"][digest]
        self.assertEqual(node["created_at"], 100.0)
        self.assertEqual(token["consumed_at"], 100.0)
        self.assertEqual(node["absolute_deadline"], 100.0 + 12 * 3600)
        self.assertEqual(response["audio_updated_at"], 100.0)
        node_internal.validate_node_authority(authority)
        write_fuse.assert_called_once_with(
            digest,
            node_id="node-0001",
            session_id=node["session_id"],
            consumed_at=100.0,
        )

    def test_identical_retry_reuses_persisted_created_at_without_wall_clock(self) -> None:
        authority = self._authority()
        digest = node_internal.hash_token("bootstrap-token")
        request = self._request()

        with (
            patch.object(node_internal, "_read_authority", return_value=authority),
            patch.object(node_internal, "_resolve_assigned_session", return_value=None),
            patch.object(node_internal, "_write_legacy_token_fuse"),
            patch.object(node_internal, "_write_authority"),
            patch.object(node_internal.time, "time", return_value=321.0),
            patch.dict("os.environ", {"NODE_EGRESS_URL": ""}),
        ):
            initial = node_internal._bootstrap_locked(request, digest)

        retry_clock = Mock(side_effect=AssertionError("retry must not read wall clock"))
        with (
            patch.object(node_internal, "_read_authority", return_value=authority),
            patch.object(node_internal.time, "time", retry_clock),
            patch.dict("os.environ", {"NODE_EGRESS_URL": ""}),
        ):
            retried = node_internal._bootstrap_locked(request, digest)

        retry_clock.assert_not_called()
        self.assertEqual(initial["node_id"], retried["node_id"])
        self.assertEqual(initial["audio_updated_at"], 321.0)
        self.assertEqual(retried["audio_updated_at"], 321.0)

    def test_invalid_explicit_response_timestamp_is_rejected(self) -> None:
        node = {
            "node_id": "node-0001",
            "session_id": "session-clock-test",
            "session_assigned": False,
            "absolute_deadline": 1000.0,
            "egress_mode": "DIRECT_PUSH",
        }
        invalid_values = (-1.0, math.nan, math.inf, -math.inf, 10**400)

        for value in invalid_values:
            with self.subTest(value=value):
                with patch.object(
                    node_internal, "_resolve_egress_delivery", return_value=("", None)
                ):
                    with self.assertRaises(node_internal.NodeStateError):
                        node_internal._bootstrap_response(
                            node,
                            None,
                            self._request().node_access_token,
                            audio_updated_at=value,
                        )

    def test_epoch_zero_remains_valid_for_bootstrap_authority_and_response(self) -> None:
        authority = self._authority()
        digest = node_internal.hash_token("bootstrap-token")
        with (
            patch.object(node_internal, "_read_authority", return_value=authority),
            patch.object(node_internal, "_resolve_assigned_session", return_value=None),
            patch.object(node_internal, "_write_legacy_token_fuse") as write_fuse,
            patch.object(node_internal, "_write_authority") as write_authority,
            patch.object(node_internal.time, "time", return_value=0.0),
            patch.dict("os.environ", {"NODE_EGRESS_URL": ""}),
        ):
            response = node_internal._bootstrap_locked(self._request(), digest)

        write_authority.assert_called_once_with(authority)
        node = authority["nodes"]["node-0001"]
        token = authority["tokens"][digest]
        self.assertEqual(node["created_at"], 0.0)
        self.assertEqual(token["consumed_at"], 0.0)
        self.assertEqual(response["audio_updated_at"], 0.0)
        node_internal.validate_node_authority(authority)
        write_fuse.assert_called_once_with(
            digest,
            node_id="node-0001",
            session_id=node["session_id"],
            consumed_at=0.0,
        )


if __name__ == "__main__":
    unittest.main()
