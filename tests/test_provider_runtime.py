from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))
sys.path.insert(0, str(ROOT))

import fake_provider_for_api as runtime  # noqa: E402
from provider.fake_provider import FakeProvider, FileFakeProvider  # noqa: E402


class ProviderRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        runtime._FAKE_PROVIDER = None

    def test_fake_is_default_and_process_singleton(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IRLIGHT_PROVIDER", None)
            os.environ.pop("IRLIGHT_FAKE_PROVIDER_STATE_FILE", None)
            first = runtime.default_provider()
            second = runtime.default_provider()
        self.assertIsInstance(first, FakeProvider)
        self.assertNotIsInstance(first, FileFakeProvider)
        self.assertIs(first, second)

    def test_file_backed_fake_can_share_inventory_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "fake-provider.json"
            with patch.dict(
                os.environ,
                {
                    "IRLIGHT_PROVIDER": "fake",
                    "IRLIGHT_FAKE_PROVIDER_STATE_FILE": str(state_file),
                },
                clear=False,
            ):
                provider = runtime.default_provider()
                self.assertIsInstance(provider, FileFakeProvider)
                self.assertIs(provider, runtime.default_provider())
                created = provider.create_volume(
                    "shared-volume",
                    100,
                    {
                        "irlight-managed": "true",
                        "irlight-session-id": "session-1",
                    },
                )

                reloaded = FileFakeProvider(state_file)
                self.assertEqual(reloaded.get_volume(created.volume_id).name, "shared-volume")
                self.assertEqual(
                    [resource.provider_id for resource in reloaded.list_managed_resources()],
                    [created.volume_id],
                )

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
