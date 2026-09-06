from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_API = ROOT / "apps" / "control-api"
sys.path.insert(0, str(CONTROL_API))

from state_safety import load_json_authority  # noqa: E402


class AuthorityJsonDuplicateKeyTest(unittest.TestCase):
    def test_duplicate_object_key_is_rejected_recursively(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
            load_json_authority(io.StringIO('{"outer":{"value":1,"value":2}}'))

    def test_unique_object_keys_are_preserved(self) -> None:
        value = load_json_authority(io.StringIO('{"outer":{"first":1,"second":2}}'))
        self.assertEqual(value, {"outer": {"first": 1, "second": 2}})

    def test_parse_constant_policy_is_preserved(self) -> None:
        def reject(value: str) -> None:
            raise ValueError(f"constant rejected: {value}")

        with self.assertRaisesRegex(ValueError, "constant rejected: NaN"):
            load_json_authority(io.StringIO('{"value":NaN}'), parse_constant=reject)

    def test_control_plane_state_readers_use_shared_authority_loader(self) -> None:
        direct_json_loaders = set()
        for path in CONTROL_API.glob("*.py"):
            if "json.load(" in path.read_text(encoding="utf-8"):
                direct_json_loaders.add(path.name)

        # destination_probe.py reads the short-lived resolver child request;
        # state_safety.py is the single implementation point for file JSON.
        self.assertEqual(
            direct_json_loaders,
            {"destination_probe.py", "state_safety.py"},
        )


if __name__ == "__main__":
    unittest.main()
