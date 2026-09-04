from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import auth_store  # noqa: E402
import catalog_store  # noqa: E402
import node_internal  # noqa: E402
from ingest_auth_guard import (  # noqa: E402
    IngestAuthGuard,
    IngestAuthGuardStateError,
)
from session_store import SessionStateError, SessionStore  # noqa: E402


class AtomicAuthorityInitializationOrderTest(unittest.TestCase):
    def test_auth_first_commit_failure_leaves_fail_closed_fuse(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            path = Path(state_dir, "users.json")
            with mock.patch.object(
                auth_store.os, "replace", side_effect=OSError("simulated crash")
            ):
                with self.assertRaises(OSError):
                    auth_store.atomic_write_json(
                        path, {"users": {}, "email_index": {}}
                    )
            self.assertTrue(Path(state_dir, ".users.json.initialized").is_file())
            with self.assertRaises(auth_store.AuthStateError):
                auth_store.read_json(path, {"users": {}, "email_index": {}})

    def test_catalog_first_commit_failure_leaves_fail_closed_fuse(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            path = Path(state_dir, "catalog.json")
            with mock.patch.object(
                catalog_store.os, "replace", side_effect=OSError("simulated crash")
            ):
                with self.assertRaises(OSError):
                    catalog_store.atomic_write_json(
                        path, {"destinations": {}, "assets": {}}
                    )
            self.assertTrue(Path(state_dir, ".catalog.json.initialized").is_file())
            with self.assertRaises(catalog_store.CatalogStateError):
                catalog_store.read_json(path, {"destinations": {}, "assets": {}})

    def test_session_first_commit_failure_leaves_fail_closed_fuse(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = SessionStore(state_dir)
            with mock.patch(
                "session_store.os.replace", side_effect=OSError("simulated crash")
            ):
                with self.assertRaises(SessionStateError):
                    store.create(user_id="user-1", environment="dev")
            self.assertTrue(Path(state_dir, ".sessions.json.initialized").is_file())
            with self.assertRaises(SessionStateError):
                SessionStore(state_dir)

    def test_ingest_guard_first_commit_failure_leaves_fail_closed_fuse(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            guard = IngestAuthGuard(state_dir)
            with mock.patch(
                "ingest_auth_guard.os.replace", side_effect=OSError("simulated crash")
            ):
                with self.assertRaises(IngestAuthGuardStateError):
                    guard.record_failure(
                        source_ip="198.51.100.10",
                        username="session-1",
                        protocol="rtmp",
                    )
            self.assertTrue(
                Path(state_dir, ".ingest_auth_guard.json.initialized").is_file()
            )
            with self.assertRaises(IngestAuthGuardStateError):
                IngestAuthGuard(state_dir)

    def test_node_authority_first_commit_failure_leaves_fail_closed_fuse(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            nodes_path = state_path / "nodes.json"
            tokens_path = state_path / "bootstrap_tokens.json"
            with (
                mock.patch.object(node_internal, "STATE_DIR", state_path),
                mock.patch.object(node_internal, "NODES_PATH", nodes_path),
                mock.patch.object(node_internal, "TOKENS_PATH", tokens_path),
            ):
                with mock.patch.object(
                    node_internal.os,
                    "replace",
                    side_effect=OSError("simulated crash"),
                ):
                    with self.assertRaises(OSError):
                        node_internal._write_authority(node_internal._default_nodes())
                self.assertTrue(
                    Path(state_dir, ".nodes.json.initialized").is_file()
                )
                with self.assertRaises(node_internal.NodeStateError):
                    node_internal.ensure_state()


if __name__ == "__main__":
    unittest.main()
