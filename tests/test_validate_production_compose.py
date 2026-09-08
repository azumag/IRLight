from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-production-compose.py"
SPEC = importlib.util.spec_from_file_location("validate_production_compose", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def valid_config() -> dict:
    return {
        "services": {
            "mediamtx": {"image": "bluenviron/mediamtx:1.20.0"},
            "continuity": {
                "image": "ghcr.io/azumag/irlight-continuity:phase-b-spike",
                "environment": {
                    "INPUT_URI_FILE": "/run/irlight/continuity-secrets/media_input_uri",
                    "EGRESS_URL_FILE": "/run/irlight/continuity-secrets/media_publish_uri",
                },
            },
            "egress-gateway": {
                "image": "ghcr.io/azumag/irlight-egress-gateway:phase-b-spike",
                "environment": {
                    "EGRESS_INPUT_URI_FILE": "/run/irlight/relay-secrets/media_relay_uri",
                    "EGRESS_URL_FILE": "/run/irlight/egress-secrets/egress_url",
                    "EGRESS_VERIFIED_PEER_IP_FILE": "/run/irlight/egress-secrets/egress_verified_peer_ip",
                },
            },
            "node-agent": {
                "image": "ghcr.io/azumag/irlight-node-agent:phase-b-spike",
                "environment": {
                    "NODE_BOOTSTRAP_TOKEN_FILE": "/run/secrets/bootstrap_token",
                },
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/var/run/docker.sock",
                        "target": "/var/run/docker.sock",
                        "read_only": True,
                    }
                ],
            },
        },
        "networks": {
            "media": {"internal": False},
            "node-auth": {"internal": True},
            "node-control": {"internal": True},
        },
        "volumes": {
            name: {
                "driver": "local",
                "driver_opts": {
                    "type": "tmpfs",
                    "device": "tmpfs",
                    "o": "size=1m,mode=700",
                },
            }
            for name in MODULE.TMPFS_SECRET_VOLUMES
        },
    }


class ProductionComposeValidatorTest(unittest.TestCase):
    def test_accepts_current_security_contract(self) -> None:
        self.assertEqual(MODULE.validate_rendered_config(valid_config()), [])

    def test_rejects_dangerous_container_settings(self) -> None:
        config = valid_config()
        config["services"]["mediamtx"].update(
            {
                "build": ".",
                "image": "example.invalid/mediamtx:latest",
                "privileged": True,
                "network_mode": "host",
                "pid": "host",
                "ipc": "host",
            }
        )

        errors = MODULE.validate_rendered_config(config)

        self.assertTrue(any("must not contain build" in error for error in errors))
        self.assertTrue(any(":latest" in error for error in errors))
        self.assertTrue(any("privileged" in error for error in errors))
        self.assertTrue(any("host network" in error for error in errors))
        self.assertTrue(any("host pid" in error for error in errors))
        self.assertTrue(any("host ipc" in error for error in errors))

    def test_rejects_secret_file_and_network_regressions(self) -> None:
        config = valid_config()
        config["services"]["egress-gateway"]["environment"]["EGRESS_URL_FILE"] = ""
        config["services"]["node-agent"]["environment"]["NODE_BOOTSTRAP_TOKEN_FILE"] = "token"
        config["networks"]["node-auth"]["internal"] = False

        errors = MODULE.validate_rendered_config(config)

        self.assertIn(
            "service egress-gateway: EGRESS_URL_FILE must reference a /run/ file",
            errors,
        )
        self.assertIn(
            "service node-agent: NODE_BOOTSTRAP_TOKEN_FILE must reference a /run/ file",
            errors,
        )
        self.assertIn("network node-auth: internal=true is required", errors)

    def test_rejects_non_tmpfs_secret_volume_and_writable_docker_socket(self) -> None:
        config = valid_config()
        config["volumes"]["irlight-egress-secrets"]["driver_opts"]["type"] = "volume"
        config["volumes"]["irlight-relay-secrets"]["driver_opts"]["o"] = "size=1m,mode=755"
        config["services"]["node-agent"]["volumes"][0]["read_only"] = False

        errors = MODULE.validate_rendered_config(config)

        self.assertIn(
            "volume irlight-egress-secrets: type=tmpfs is required",
            errors,
        )
        self.assertIn(
            "volume irlight-relay-secrets: mode=700 is required",
            errors,
        )
        self.assertIn(
            "service node-agent: Docker socket must be mounted read-only",
            errors,
        )


if __name__ == "__main__":
    unittest.main()
