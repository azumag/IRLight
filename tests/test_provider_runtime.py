from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))
sys.path.insert(0, str(ROOT))

import fake_provider_for_api as runtime  # noqa: E402
from provider.fake_provider import FakeProvider  # noqa: E402


class ProviderRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        runtime._FAKE_PROVIDER = None

    def test_fake_is_default_and_process_singleton(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IRLIGHT_PROVIDER", None)
            first = runtime.default_provider()
            second = runtime.default_provider()
        self.assertIsInstance(first, FakeProvider)
        self.assertIs(first, second)

    def test_rejects_unknown_provider_mode(self) -> None:
        with patch.dict(os.environ, {"IRLIGHT_PROVIDER": "other"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "unsupported IRLIGHT_PROVIDER"):
                runtime.default_provider()

    @patch("fake_provider_for_api.ConohaClient")
    @patch("fake_provider_for_api.ConohaConfig.from_env")
    def test_conoha_builds_fresh_client_from_environment(self, from_env, client) -> None:
        config = object()
        first_client = object()
        second_client = object()
        from_env.return_value = config
        client.side_effect = [first_client, second_client]

        with patch.dict(os.environ, {"IRLIGHT_PROVIDER": "conoha"}, clear=False):
            first = runtime.default_provider()
            second = runtime.default_provider()

        self.assertIs(first, first_client)
        self.assertIs(second, second_client)
        self.assertEqual(from_env.call_count, 2)
        self.assertEqual(client.call_count, 2)
        client.assert_any_call(config)


if __name__ == "__main__":
    unittest.main()
