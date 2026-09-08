from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from auth_store import _default_sessions, _default_users  # noqa: E402
from control_store import default_control  # noqa: E402
import state_restore_compare_cli as restore_compare  # noqa: E402
from state_restore_compare_cli import (  # noqa: E402
    compare_state_snapshots,
    main as compare_main,
)
from state_safety import initialization_marker  # noqa: E402


class StateRestoreCompareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="irlight-restore-compare-")
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.candidate = self.root / "candidate"
        self.source.mkdir()
        self._write_authority(self.source / "control.json", default_control(now=1.0))
        self._write_authority(
            self.source / "catalog.json", {"destinations": {}, "assets": {}}
        )
        self._write_authority(self.source / "users.json", _default_users())
        self._write_authority(self.source / "auth_sessions.json", _default_sessions())
        self._write_authority(
            self.source / "nodes.json",
            {"nodes": {}, "next_node_seq": 1, "tokens": {}},
        )
        shutil.copytree(self.source, self.candidate)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _write_authority(path: Path, payload: dict[str, object]) -> None:
        path.write_text(
            json.dumps(payload, allow_nan=False, sort_keys=True), encoding="utf-8"
        )
        initialization_marker(path).write_text("v1\n", encoding="utf-8")

    def _snapshot(self) -> dict[str, tuple[bytes, int]]:
        result: dict[str, tuple[bytes, int]] = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                stat_result = path.stat()
                result[str(path.relative_to(self.root))] = (
                    path.read_bytes(),
                    stat_result.st_mtime_ns,
                )
        return result

    def _compare(self) -> dict[str, object]:
        return compare_state_snapshots(
            source_state_dir=self.source,
            candidate_state_dir=self.candidate,
        )

    def test_identical_valid_snapshots_match_without_mutation(self) -> None:
        before = self._snapshot()
        payload = self._compare()

        self.assertEqual(payload["status"], "MATCH")
        self.assertIsNone(payload["reason_code"])
        self.assertTrue(all(item["status"] == "MATCH" for item in payload["checks"]))
        self.assertEqual(self._snapshot(), before)
        self.assertFalse((self.source / ".control-state.lock").exists())
        self.assertFalse((self.candidate / ".control-state.lock").exists())


    def test_separate_node_roots_use_their_verified_directory_fds(self) -> None:
        source_node = self.root / "source-node"
        candidate_node = self.root / "candidate-node"
        source_node.mkdir()
        candidate_node.mkdir()
        for state_dir, node_dir in (
            (self.source, source_node),
            (self.candidate, candidate_node),
        ):
            nodes = state_dir / "nodes.json"
            marker = initialization_marker(nodes)
            nodes.rename(node_dir / nodes.name)
            marker.rename(node_dir / marker.name)

        payload = compare_state_snapshots(
            source_state_dir=self.source,
            candidate_state_dir=self.candidate,
            source_node_state_dir=source_node,
            candidate_node_state_dir=candidate_node,
        )

        self.assertEqual(payload["status"], "MATCH")
        self.assertTrue(all(item["status"] == "MATCH" for item in payload["checks"]))

    def test_shared_state_and_node_root_is_opened_once_per_side(self) -> None:
        original = restore_compare._open_snapshot_root
        with mock.patch.object(
            restore_compare, "_open_snapshot_root", wraps=original
        ) as open_root:
            payload = self._compare()

        self.assertEqual(payload["status"], "MATCH")
        self.assertEqual(open_root.call_count, 4)
        self.assertEqual(
            [call.args[0] for call in open_root.call_args_list],
            [self.source, self.candidate, self.source, self.candidate],
        )

    def test_root_replacement_after_open_fails_closed_without_retargeting(self) -> None:
        opened_candidate = self.root / "candidate-opened"
        original = restore_compare._validated_fingerprint_at
        swapped = False

        def fingerprint_with_replacement(*args: object, **kwargs: object):
            nonlocal swapped
            result = original(*args, **kwargs)
            if not swapped:
                self.candidate.rename(opened_candidate)
                shutil.copytree(self.source, self.candidate)
                self._write_authority(
                    self.candidate / "control.json", default_control(now=2.0)
                )
                swapped = True
            return result

        with mock.patch.object(
            restore_compare,
            "_validated_fingerprint_at",
            side_effect=fingerprint_with_replacement,
        ):
            payload = self._compare()

        self.assertTrue(swapped)
        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertEqual(payload["reason_code"], "SNAPSHOT_ROOT_UNAVAILABLE")
        self.assertEqual(payload["checks"], [])
        self.assertNotEqual(
            (opened_candidate / "control.json").read_bytes(),
            (self.candidate / "control.json").read_bytes(),
        )

    def test_same_snapshot_root_is_rejected_as_not_distinct(self) -> None:
        before = self._snapshot()
        payload = compare_state_snapshots(
            source_state_dir=self.source,
            candidate_state_dir=self.source,
        )

        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertEqual(payload["reason_code"], "SOURCE_CANDIDATE_NOT_DISTINCT")
        self.assertEqual(payload["checks"], [])
        self.assertEqual(self._snapshot(), before)

    def test_shared_node_root_is_rejected_as_not_distinct(self) -> None:
        payload = compare_state_snapshots(
            source_state_dir=self.source,
            candidate_state_dir=self.candidate,
            source_node_state_dir=self.source,
            candidate_node_state_dir=self.source,
        )

        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertEqual(payload["reason_code"], "SOURCE_CANDIDATE_NOT_DISTINCT")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_symlinked_candidate_snapshot_root_is_rejected(self) -> None:
        candidate_link = self.root / "candidate-link"
        candidate_link.symlink_to(self.candidate, target_is_directory=True)
        before = self._snapshot()

        payload = compare_state_snapshots(
            source_state_dir=self.source,
            candidate_state_dir=candidate_link,
        )

        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertEqual(payload["reason_code"], "SNAPSHOT_ROOT_UNAVAILABLE")
        self.assertEqual(payload["checks"], [])
        self.assertEqual(self._snapshot(), before)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_symlinked_source_snapshot_root_is_rejected(self) -> None:
        source_link = self.root / "source-link"
        source_link.symlink_to(self.source, target_is_directory=True)

        payload = compare_state_snapshots(
            source_state_dir=source_link,
            candidate_state_dir=self.candidate,
        )

        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertEqual(payload["reason_code"], "SNAPSHOT_ROOT_UNAVAILABLE")
        self.assertEqual(payload["checks"], [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_symlinked_candidate_snapshot_parent_is_rejected(self) -> None:
        linked_parent = self.root / "candidate-parent-link"
        linked_parent.symlink_to(self.root, target_is_directory=True)
        candidate_through_parent = linked_parent / "candidate"
        before = self._snapshot()

        payload = compare_state_snapshots(
            source_state_dir=self.source,
            candidate_state_dir=candidate_through_parent,
        )

        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertEqual(payload["reason_code"], "SNAPSHOT_ROOT_UNAVAILABLE")
        self.assertEqual(payload["checks"], [])
        self.assertEqual(self._snapshot(), before)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_symlinked_source_snapshot_parent_is_rejected(self) -> None:
        linked_parent = self.root / "source-parent-link"
        linked_parent.symlink_to(self.root, target_is_directory=True)
        source_through_parent = linked_parent / "source"

        payload = compare_state_snapshots(
            source_state_dir=source_through_parent,
            candidate_state_dir=self.candidate,
        )

        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertEqual(payload["reason_code"], "SNAPSHOT_ROOT_UNAVAILABLE")
        self.assertEqual(payload["checks"], [])

    def test_valid_content_difference_is_redacted_mismatch(self) -> None:
        dummy_secret = "AUDIT_DUMMY_RESTORE_SECRET"
        candidate_catalog = self.candidate / "catalog.json"
        candidate_catalog.write_text(
            json.dumps(
                {
                    "destinations": {},
                    "assets": {"asset-1": {"note": dummy_secret}},
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = compare_main(
                [
                    "--source-state-dir",
                    str(self.source),
                    "--candidate-state-dir",
                    str(self.candidate),
                ]
            )

        self.assertEqual(result, 2)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "MISMATCH")
        catalog = next(item for item in payload["checks"] if item["authority"] == "catalog")
        self.assertEqual(catalog["reason_code"], "AUTHORITY_CONTENT_MISMATCH")
        self.assertNotIn(dummy_secret, output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_missing_candidate_authority_is_unavailable_and_not_recreated(self) -> None:
        candidate_users = self.candidate / "users.json"
        candidate_users.unlink()
        before = self._snapshot()

        payload = self._compare()

        self.assertEqual(payload["status"], "UNAVAILABLE")
        users = next(item for item in payload["checks"] if item["authority"] == "users")
        self.assertEqual(users["reason_code"], "CANDIDATE_AUTHORITY_UNAVAILABLE")
        self.assertFalse(candidate_users.exists())
        self.assertTrue(initialization_marker(candidate_users).exists())
        self.assertEqual(self._snapshot(), before)

    def test_missing_source_authority_reports_source_unavailable(self) -> None:
        (self.source / "control.json").unlink()

        payload = self._compare()

        control = next(item for item in payload["checks"] if item["authority"] == "control")
        self.assertEqual(control["status"], "UNAVAILABLE")
        self.assertEqual(control["reason_code"], "SOURCE_AUTHORITY_UNAVAILABLE")

    def test_legacy_token_fuse_presence_mismatch_is_detected(self) -> None:
        legacy = self.source / "bootstrap_tokens.json"
        legacy.write_text(
            json.dumps(
                {
                    "tokens": {
                        "a" * 64: {
                            "consumed": True,
                            "consumed_at": 1.0,
                            "node_id": "node-1",
                            "session_id": "session-1",
                        }
                    }
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        initialization_marker(legacy).write_text("v1\n", encoding="utf-8")

        payload = self._compare()

        legacy_check = next(
            item for item in payload["checks"] if item["authority"] == "legacy_bootstrap_tokens"
        )
        self.assertEqual(legacy_check["status"], "MISMATCH")
        self.assertEqual(legacy_check["reason_code"], "LEGACY_FUSE_PRESENCE_MISMATCH")

    def test_legacy_token_marker_loss_is_detected_even_when_file_matches(self) -> None:
        legacy_payload = {"tokens": {}}
        source_legacy = self.source / "bootstrap_tokens.json"
        candidate_legacy = self.candidate / "bootstrap_tokens.json"
        source_legacy.write_text(json.dumps(legacy_payload), encoding="utf-8")
        candidate_legacy.write_text(json.dumps(legacy_payload), encoding="utf-8")
        initialization_marker(source_legacy).write_text("v1\n", encoding="utf-8")

        payload = self._compare()

        legacy_check = next(
            item for item in payload["checks"] if item["authority"] == "legacy_bootstrap_tokens"
        )
        self.assertEqual(legacy_check["reason_code"], "LEGACY_FUSE_MARKER_MISMATCH")

    @unittest.skipUnless(hasattr(os, "link"), "hard links are unavailable")
    def test_hardlinked_candidate_authority_is_rejected_as_not_distinct(self) -> None:
        candidate_users = self.candidate / "users.json"
        candidate_users.unlink()
        os.link(self.source / "users.json", candidate_users)

        payload = self._compare()

        users = next(item for item in payload["checks"] if item["authority"] == "users")
        self.assertEqual(users["status"], "UNAVAILABLE")
        self.assertEqual(
            users["reason_code"], "SOURCE_CANDIDATE_AUTHORITY_NOT_DISTINCT"
        )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_symlinked_candidate_authority_is_rejected(self) -> None:
        target = self.root / "replacement-users.json"
        target.write_text((self.source / "users.json").read_text(encoding="utf-8"), encoding="utf-8")
        candidate_users = self.candidate / "users.json"
        candidate_users.unlink()
        candidate_users.symlink_to(target)

        payload = self._compare()

        users = next(item for item in payload["checks"] if item["authority"] == "users")
        self.assertEqual(users["status"], "UNAVAILABLE")
        self.assertEqual(users["reason_code"], "CANDIDATE_AUTHORITY_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
