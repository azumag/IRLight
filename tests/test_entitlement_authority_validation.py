from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from entitlement_store import EntitlementStateError, EntitlementStore  # noqa: E402


class EntitlementAuthorityValidationTest(unittest.TestCase):
    def _write_payload(self, state_dir: str, payload: object) -> Path:
        path = Path(state_dir, "entitlements.json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _record(self, **changes: object) -> dict[str, object]:
        record: dict[str, object] = {
            "id": "user:user-a",
            "user_id": "user-a",
            "plan": "supporter",
            "max_concurrent_sessions": 3,
            "updated_at": 1234.5,
        }
        record.update(changes)
        return record

    def test_valid_persisted_record_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            self._write_payload(
                state_dir,
                {"entitlements": {"user-a": self._record()}},
            )
            entitlement = EntitlementStore(state_dir).get("user-a")
            self.assertEqual(entitlement["plan"], "supporter")
            self.assertEqual(entitlement["max_concurrent_sessions"], 3)

    def test_non_finite_json_constants_fail_closed_without_rewrite(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as state_dir:
                path = Path(state_dir, "entitlements.json")
                original = (
                    '{"entitlements":{"user-a":{"id":"user:user-a","user_id":"user-a",'
                    '"plan":"supporter","max_concurrent_sessions":3,"updated_at":'
                    + value
                    + "}}}"
                )
                path.write_text(original, encoding="utf-8")

                with self.assertRaises(EntitlementStateError):
                    EntitlementStore(state_dir)

                self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_invalid_record_fields_fail_closed(self) -> None:
        invalid_records = [
            self._record(id="user:other"),
            self._record(user_id="other"),
            self._record(plan=""),
            self._record(plan="   "),
            self._record(max_concurrent_sessions=True),
            self._record(max_concurrent_sessions=-1),
            self._record(max_concurrent_sessions=1.5),
            self._record(max_concurrent_sessions="3"),
            self._record(updated_at=True),
            self._record(updated_at=None),
            self._record(updated_at="1234"),
            self._record(updated_at=10**1000),
        ]
        for record in invalid_records:
            with self.subTest(record=record), tempfile.TemporaryDirectory() as state_dir:
                self._write_payload(state_dir, {"entitlements": {"user-a": record}})
                with self.assertRaises(EntitlementStateError):
                    EntitlementStore(state_dir)

    def test_missing_required_fields_fail_closed(self) -> None:
        for field in (
            "id",
            "user_id",
            "plan",
            "max_concurrent_sessions",
            "updated_at",
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as state_dir:
                record = self._record()
                record.pop(field)
                self._write_payload(state_dir, {"entitlements": {"user-a": record}})
                with self.assertRaises(EntitlementStateError):
                    EntitlementStore(state_dir)

    def test_writer_rejects_non_finite_timestamp_without_replacing_authority(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = EntitlementStore(state_dir)
            store.set("user-a", max_concurrent_sessions=2, plan="supporter")
            path = Path(state_dir, "entitlements.json")
            original = path.read_bytes()

            with patch("entitlement_store.time.time", return_value=float("nan")):
                with self.assertRaises(EntitlementStateError):
                    store.set("user-b", max_concurrent_sessions=1, plan="default")

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(
                EntitlementStore(state_dir).get("user-a")["max_concurrent_sessions"],
                2,
            )

    def test_set_rejects_type_confusion_before_persist(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = EntitlementStore(state_dir)
            invalid = [
                ("", 1, "default"),
                ("user-a", True, "default"),
                ("user-a", 1.0, "default"),
                ("user-a", -1, "default"),
                ("user-a", 1, ""),
                ("user-a", 1, "   "),
            ]
            for user_id, limit, plan in invalid:
                with self.subTest(user_id=user_id, limit=limit, plan=plan):
                    with self.assertRaises(ValueError):
                        store.set(
                            user_id,
                            max_concurrent_sessions=limit,  # type: ignore[arg-type]
                            plan=plan,
                        )


if __name__ == "__main__":
    unittest.main()
