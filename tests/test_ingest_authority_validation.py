from __future__ import annotations

import hashlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from ingest_store import IngestCredentialError, IngestCredentialStore  # noqa: E402


def _record(secret: str = "publisher-secret") -> dict[str, object]:
    return {
        "id": "credential-1",
        "session_id": "session-1",
        "user_id": "user-1",
        "scope": "INGEST",
        "username": "session-1",
        "secret_sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
        "protocols": ["rtmp", "srt"],
        "created_at": 100.0,
        "expires_at": 200.0,
        "revoked_at": None,
        "last_authenticated_at": None,
    }


def _write_state(state_dir: str, record: dict[str, object]) -> Path:
    path = Path(state_dir, "ingest_credentials.json")
    path.write_text(
        json.dumps({"credentials": {"credential-1": record}}),
        encoding="utf-8",
    )
    return path


class IngestAuthorityValidationTest(unittest.TestCase):
    def test_non_finite_persisted_timestamps_fail_closed(self) -> None:
        for field in (
            "created_at",
            "expires_at",
            "revoked_at",
            "last_authenticated_at",
        ):
            for value in (math.nan, math.inf, -math.inf):
                with self.subTest(field=field, value=value):
                    with tempfile.TemporaryDirectory() as state_dir:
                        record = _record()
                        record[field] = value
                        _write_state(state_dir, record)
                        with self.assertRaises(IngestCredentialError):
                            IngestCredentialStore(state_dir)

    def test_oversized_integer_timestamp_is_a_controlled_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            record = _record()
            record["expires_at"] = 10**10000
            _write_state(state_dir, record)
            with self.assertRaises(IngestCredentialError):
                IngestCredentialStore(state_dir)

    def test_timestamp_type_confusion_and_missing_required_fields_fail_closed(self) -> None:
        for field in ("created_at", "expires_at"):
            for value in (True, None, "100"):
                with self.subTest(field=field, value=value):
                    with tempfile.TemporaryDirectory() as state_dir:
                        record = _record()
                        record[field] = value
                        _write_state(state_dir, record)
                        with self.assertRaises(IngestCredentialError):
                            IngestCredentialStore(state_dir)

        required = (
            "id",
            "session_id",
            "user_id",
            "username",
            "secret_sha256",
            "protocols",
            "created_at",
            "expires_at",
        )
        for field in required:
            with self.subTest(missing=field):
                with tempfile.TemporaryDirectory() as state_dir:
                    record = _record()
                    record.pop(field)
                    _write_state(state_dir, record)
                    with self.assertRaises(IngestCredentialError):
                        IngestCredentialStore(state_dir)

    def test_identity_scope_protocol_and_digest_invariants_are_validated(self) -> None:
        valid_digest = str(_record()["secret_sha256"])
        whitespace_digest = valid_digest[:62] + " \n"
        mutations = {
            "id/key mismatch": ("id", "credential-2"),
            "username/session mismatch": ("username", "other-session"),
            "unknown scope": ("scope", "UNKNOWN"),
            "unsupported protocol": ("protocols", ["rtsp"]),
            "invalid digest": ("secret_sha256", "not-a-sha256"),
            "digest with whitespace": ("secret_sha256", whitespace_digest),
            "non-positive lifetime": ("expires_at", 100.0),
        }
        for name, (field, value) in mutations.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as state_dir:
                    record = _record()
                    record[field] = value
                    _write_state(state_dir, record)
                    with self.assertRaises(IngestCredentialError):
                        IngestCredentialStore(state_dir)

    def test_pre_relay_record_without_scope_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            secret = "legacy-secret"
            record = _record(secret)
            record.pop("scope")
            _write_state(state_dir, record)

            store = IngestCredentialStore(state_dir)
            loaded = store.get("credential-1")
            self.assertIsNotNone(loaded)
            self.assertIsNotNone(
                store.verify(
                    username="session-1",
                    secret=secret,
                    protocol="rtmp",
                    scope="INGEST",
                    now=150.0,
                )
            )

    def test_invalid_authority_is_not_rewritten_or_marked_initialized(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            record = _record()
            record["expires_at"] = "Infinity"
            path = _write_state(state_dir, record)
            before = path.read_bytes()

            with self.assertRaises(IngestCredentialError):
                IngestCredentialStore(state_dir)

            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(
                Path(state_dir, ".ingest_credentials.json.initialized").exists()
            )

    def test_issue_rejects_non_finite_or_oversized_time_inputs_before_persisting(self) -> None:
        for field, kwargs in (
            ("ttl_seconds", {"ttl_seconds": math.nan}),
            ("ttl_seconds", {"ttl_seconds": math.inf}),
            ("ttl_seconds", {"ttl_seconds": 10**10000}),
            ("now", {"now": math.nan}),
            ("now", {"now": math.inf}),
            ("now", {"now": 10**10000}),
        ):
            with self.subTest(field=field, kwargs=kwargs):
                with tempfile.TemporaryDirectory() as state_dir:
                    store = IngestCredentialStore(state_dir)
                    with self.assertRaises(ValueError):
                        store.issue(
                            session_id="session-1",
                            user_id="user-1",
                            protocols=["rtmp"],
                            **kwargs,
                        )
                    self.assertFalse(Path(state_dir, "ingest_credentials.json").exists())


if __name__ == "__main__":
    unittest.main()
