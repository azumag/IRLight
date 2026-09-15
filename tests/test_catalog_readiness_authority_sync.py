from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_API = ROOT / "apps" / "control-api"


class CatalogReadinessAuthoritySyncTest(unittest.TestCase):
    def test_readyz_uses_catalog_record_authority_validation_without_repair(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-catalog-readyz-") as temporary:
            state_dir = Path(temporary) / "control"
            node_state_dir = Path(temporary) / "node"
            state_dir.mkdir()
            node_state_dir.mkdir()

            env = os.environ.copy()
            env["STATE_DIR"] = str(state_dir)
            env["NODE_STATE_DIR"] = str(node_state_dir)
            env["PYTHONPATH"] = str(CONTROL_API)

            script = textwrap.dedent(
                """
                import json
                from pathlib import Path

                import app as control_app
                from fastapi import HTTPException

                catalog_path = control_app.STATE_DIR / "catalog.json"
                sentinel = "AUDIT_DUMMY_CATALOG_SECRET"

                cases = (
                    {
                        "destinations": {
                            "destination-1": {
                                "id": "different-id",
                                "user_id": "user-1",
                                "type": "rtmp",
                                "display_name": "Example",
                                "server_url": "rtmp://example.invalid/live",
                                "secret_ref": "secret/example",
                            }
                        },
                        "assets": {},
                    },
                    {
                        "destinations": {
                            "destination-1": {
                                "id": "destination-1",
                                "user_id": None,
                                "type": "rtmp",
                                "display_name": "Example",
                                "server_url": "rtmp://example.invalid/live",
                                "secret_ref": sentinel,
                            }
                        },
                        "assets": {},
                    },
                    {
                        "destinations": {},
                        "assets": {
                            "asset-1": {
                                "id": "asset-1",
                                "user_id": "user-1",
                                "source_object_key": ["uploads/standby.png"],
                            }
                        },
                    },
                )

                for payload in cases:
                    catalog_path.write_text(json.dumps(payload), encoding="utf-8")
                    before = catalog_path.read_bytes()
                    try:
                        control_app.readyz()
                    except HTTPException as exc:
                        assert exc.status_code == 503
                        assert exc.detail == {"code": "STATE_AUTHORITY_UNAVAILABLE"}
                        rendered = repr(exc.detail)
                        assert sentinel not in rendered
                        assert str(catalog_path) not in rendered
                    else:
                        raise AssertionError("malformed catalog unexpectedly reported ready")
                    assert catalog_path.read_bytes() == before
                """
            )

            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(
                completed.returncode,
                0,
                msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
