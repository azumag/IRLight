from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from ingest_auth_proxy import INTERNAL_MEDIA_ACTIONS, IngestAuthProxy  # noqa: E402
import node_entrypoint  # noqa: E402
from node_entrypoint import (  # noqa: E402
    prepare_internal_media_auth,
    remove_internal_media_auth,
)


class InternalMediaAuthTest(unittest.TestCase):
    def test_internal_secret_authorizes_only_exact_media_actions(self) -> None:
        proxy = IngestAuthProxy(
            upstream_url="http://127.0.0.1:1/unreachable",
            internal_media_secret="internal-secret",
        )
        base = {
            "user": "irlight-internal",
            "password": "internal-secret",
            "protocol": "rtmp",
            "action": "publish",
            "path": "output/relay",
        }
        allowed = proxy.authorize(base)
        self.assertEqual(allowed.status, 200)
        self.assertTrue(json.loads(allowed.body)["authorized"])

        wrong_path = proxy.authorize({**base, "path": "live/input"})
        self.assertEqual(wrong_path.status, 403)
        wrong_secret = proxy.authorize({**base, "password": "wrong"})
        self.assertEqual(wrong_secret.status, 401)

    def test_internal_secret_cannot_be_reused_for_another_action(self) -> None:
        secrets_by_action = {
            ("rtmp", "publish", "output/relay"): "publish-secret",
            ("rtsp", "read", "live/input"): "input-secret",
            ("rtsp", "read", "output/relay"): "relay-secret",
        }
        proxy = IngestAuthProxy(
            upstream_url="http://127.0.0.1:1/unreachable",
            internal_media_secrets=secrets_by_action,
        )
        cross_action = proxy.authorize(
            {
                "user": "irlight-internal",
                "password": "relay-secret",
                "protocol": "rtsp",
                "action": "read",
                "path": "live/input",
            }
        )
        self.assertEqual(cross_action.status, 401)

    def test_internal_urls_are_written_once_to_private_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secret_dir = Path(tmp) / "secrets"
            action_secrets, paths = prepare_internal_media_auth(secret_dir)
            try:
                self.assertEqual(len(paths), 3)
                self.assertEqual(set(action_secrets), INTERNAL_MEDIA_ACTIONS)
                self.assertEqual(len(set(action_secrets.values())), 3)
                for path in paths:
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    self.assertTrue(
                        any(
                            secret in path.read_text(encoding="utf-8")
                            for secret in action_secrets.values()
                        )
                    )
                self.assertEqual(stat.S_IMODE(secret_dir.stat().st_mode), 0o700)
            finally:
                remove_internal_media_auth(paths)
            self.assertTrue(all(not path.exists() for path in paths))

    def test_production_config_does_not_bypass_relay_auth(self) -> None:
        config = (ROOT / "config" / "mediamtx.yml").read_text(encoding="utf-8")
        auth_excludes = config.split("authHTTPExclude:", 1)[1].split("rtsp:", 1)[0]
        self.assertNotIn("action: read", auth_excludes)
        self.assertNotIn("action: publish", auth_excludes)
        relay = config.split("output/relay:", 1)[1]
        self.assertIn("overridePublisher: false", relay)
        self.assertIn("maxReaders: 1", relay)

    def test_entrypoint_removes_internal_files_when_proxy_start_fails(self) -> None:
        def run_lifecycle(**kwargs):
            kwargs["pre_media_start"]("node-access-token")
            return 0

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {
                "NODE_CONTROL_PLANE_URL": "http://control.invalid",
                "NODE_MEDIA_SECRET_DIR": tmp,
                "NODE_RELAY_SECRET_DIR": tmp,
            },
            clear=False,
        ), patch.object(
            node_entrypoint.IngestAuthProxy,
            "start",
            side_effect=RuntimeError("bind failed"),
        ), patch.object(
            node_entrypoint,
            "agent_main",
            side_effect=run_lifecycle,
        ):
            with self.assertRaisesRegex(RuntimeError, "bind failed"):
                node_entrypoint.main()

            self.assertTrue(
                all(not (Path(tmp) / name).exists() for name in node_entrypoint.INTERNAL_SECRET_FILES)
            )

    def test_production_mounts_keep_destination_secret_out_of_continuity(self) -> None:
        config = (ROOT / "docker-compose.node.yml").read_text(encoding="utf-8")
        continuity = config.split("  continuity:", 1)[1].split(
            "  egress-gateway:", 1
        )[0]
        self.assertIn("irlight-continuity-secrets", continuity)
        self.assertNotIn("irlight-egress-secrets", continuity)
        self.assertNotIn("egress_url", continuity)


if __name__ == "__main__":
    unittest.main()
