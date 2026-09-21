from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_API = ROOT / "apps" / "control-api"


class CatalogTimestampAuthorityTest(unittest.TestCase):
    def test_catalog_record_timestamps_fail_closed_without_repair(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-catalog-timestamps-") as temporary:
            env = os.environ.copy()
            env["STATE_DIR"] = temporary
            env["PYTHONPATH"] = str(CONTROL_API)

            script = textwrap.dedent(
                """
                import copy
                import json
                from unittest.mock import patch

                import catalog_store

                catalog_store.ensure_catalog()
                destination = catalog_store.create_destination(
                    user_id="user-1",
                    type="rtmp",
                    display_name="Example",
                    server_url="rtmp://example.invalid/live",
                    secret_ref="secret/example",
                )
                asset = catalog_store.create_asset(
                    user_id="user-1",
                    source_object_key="uploads/standby.png",
                )
                clean = json.loads(catalog_store.CATALOG_PATH.read_text(encoding="utf-8"))

                targets = (
                    ("destinations", destination["id"], catalog_store.list_destinations),
                    ("assets", asset["id"], catalog_store.list_assets),
                )
                bad_values = (True, None, "123", -1, 10**1000)

                for kind, record_id, reader in targets:
                    for field in ("created_at", "updated_at"):
                        for bad_value in bad_values:
                            damaged = copy.deepcopy(clean)
                            damaged[kind][record_id][field] = bad_value
                            catalog_store.CATALOG_PATH.write_text(
                                json.dumps(damaged), encoding="utf-8"
                            )
                            before = catalog_store.CATALOG_PATH.read_bytes()
                            try:
                                reader("user-1")
                            except catalog_store.CatalogStateError:
                                pass
                            else:
                                raise AssertionError(
                                    f"{kind}.{field} accepted invalid value {bad_value!r}"
                                )
                            assert catalog_store.CATALOG_PATH.read_bytes() == before

                        damaged = copy.deepcopy(clean)
                        damaged[kind][record_id].pop(field)
                        catalog_store.CATALOG_PATH.write_text(
                            json.dumps(damaged), encoding="utf-8"
                        )
                        before = catalog_store.CATALOG_PATH.read_bytes()
                        try:
                            reader("user-1")
                        except catalog_store.CatalogStateError:
                            pass
                        else:
                            raise AssertionError(f"{kind}.{field} accepted a missing field")
                        assert catalog_store.CATALOG_PATH.read_bytes() == before

                damaged = copy.deepcopy(clean)
                damaged["destinations"][destination["id"]]["last_verified_at"] = -1
                catalog_store.CATALOG_PATH.write_text(
                    json.dumps(damaged), encoding="utf-8"
                )
                before = catalog_store.CATALOG_PATH.read_bytes()
                try:
                    catalog_store.list_destinations("user-1")
                except catalog_store.CatalogStateError:
                    pass
                else:
                    raise AssertionError("negative last_verified_at was accepted")
                assert catalog_store.CATALOG_PATH.read_bytes() == before

                catalog_store.CATALOG_PATH.write_text(json.dumps(clean), encoding="utf-8")

                def verify_success():
                    return catalog_store.verify_destination(
                        destination["id"],
                        "user-1",
                        probe=lambda _url: {
                            "protocol": "rtmp",
                            "peer_ip": "192.0.2.10",
                            "peer_port": 1935,
                            "elapsed_ms": 1,
                        },
                    )

                writer_calls = (
                    lambda: catalog_store.create_destination(
                        user_id="user-1",
                        type="rtmp",
                        display_name="Invalid clock",
                        server_url="rtmp://clock.invalid/live",
                        secret_ref="secret/clock",
                    ),
                    lambda: catalog_store.create_asset(
                        user_id="user-1",
                        source_object_key="uploads/invalid-clock.png",
                    ),
                    lambda: catalog_store.update_destination(
                        destination["id"],
                        user_id="user-1",
                        display_name="Changed",
                    ),
                    verify_success,
                    lambda: catalog_store._record_verification_failure(
                        destination["id"],
                        "user-1",
                        "rtmp://example.invalid/live",
                        "probe failed",
                    ),
                )
                bad_clocks = (-1.0, float("nan"), float("inf"), float("-inf"), 10**1000)

                for bad_clock in bad_clocks:
                    for writer in writer_calls:
                        before = catalog_store.CATALOG_PATH.read_bytes()
                        with patch("catalog_store.time.time", return_value=bad_clock):
                            try:
                                writer()
                            except catalog_store.CatalogStateError:
                                pass
                            else:
                                raise AssertionError(
                                    f"catalog writer accepted invalid clock {bad_clock!r}"
                                )
                        assert catalog_store.CATALOG_PATH.read_bytes() == before

                with patch("catalog_store.time.time", return_value=0.0):
                    epoch_asset = catalog_store.create_asset(
                        user_id="user-1",
                        source_object_key="uploads/epoch.png",
                    )
                assert epoch_asset["created_at"] == 0.0
                assert epoch_asset["updated_at"] == 0.0
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
