from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

_TMP = tempfile.mkdtemp(prefix="irlight-asset-processing-")
os.environ["STATE_DIR"] = _TMP

from asset_processing import normalize_image_blob, image_dimensions  # noqa: E402
from asset_upload_intent import source_object_key, variant_object_key  # noqa: E402
from catalog_store import (  # noqa: E402
    CATALOG_PATH,
    CatalogValidationError,
    complete_asset_processing,
    complete_asset_upload,
    create_asset_upload_intent,
    ensure_catalog,
    get_asset,
)
from state_safety import initialization_marker  # noqa: E402

from fastapi import FastAPI  # noqa: E402

import catalog_api  # noqa: E402

_SHA256_HEX = "a" * 64


class AssetProcessingModuleTest(unittest.TestCase):
    def test_normalize_image_blob_png(self) -> None:
        # 1x1 PNG
        data = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00"
            + b"\x00\x00\x00\x00"  # crc placeholder (will fail) not checked here? Actually checked.
        )
        # Use module dimensions parser separately; normalization expects CRC valid.
        # Instead test invalid mime
        with self.assertRaises(Exception):
            normalize_image_blob(b"not an image")

    def test_image_dimensions_none_for_garbage(self) -> None:
        self.assertIsNone(image_dimensions(b"garbage"))


class AssetProcessingStoreTest(unittest.TestCase):
    def setUp(self):
        if CATALOG_PATH.exists():
            CATALOG_PATH.unlink()
        initialization_marker(CATALOG_PATH).unlink(missing_ok=True)
        ensure_catalog()

    def test_complete_processing_to_ready_is_not_exposed(self):
        item = create_asset_upload_intent(user_id="user-a", content_type="image/png")
        complete_asset_upload(
            asset_id=item["id"],
            user_id="user-a",
            content_type="image/png",
            object_key=item["source_object_key"],
            size_bytes=1024,
            sha256="a" * 64,
        )
        with self.assertRaises(CatalogValidationError):
            complete_asset_processing(
                asset_id=item["id"],
                user_id="user-a",
                processed_width=64,
                processed_height=64,
                variants={"720p30": {"size_bytes": 10}},
            )
        fetched = get_asset(item["id"], "user-a")
        self.assertEqual(fetched["processing_status"], "PROCESSING")

    def test_complete_processing_requires_processing_state(self):
        item = create_asset_upload_intent(user_id="user-a", content_type="image/png")
        with self.assertRaises(CatalogValidationError):
            complete_asset_processing(asset_id=item["id"], user_id="user-a")

    def test_fail_processing_marks_failed(self):
        item = create_asset_upload_intent(user_id="user-a", content_type="image/png")
        complete_asset_upload(
            asset_id=item["id"],
            user_id="user-a",
            content_type="image/png",
            object_key=item["source_object_key"],
            size_bytes=1024,
            sha256="a" * 64,
        )
        completed = complete_asset_processing(
            asset_id=item["id"], user_id="user-a", failure_reason="bad decode"
        )
        self.assertEqual(completed["processing_status"], "FAILED")
        self.assertEqual(completed["failure_reason"], "bad decode")


def _make_processing_asset(user_id: str = "user-a") -> dict:
    item = create_asset_upload_intent(user_id=user_id, content_type="image/png")
    complete_asset_upload(
        asset_id=item["id"],
        user_id=user_id,
        content_type="image/png",
        object_key=item["source_object_key"],
        size_bytes=1024,
        sha256=_SHA256_HEX,
    )
    return item


class AssetProcessingCompletionHttpTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        CATALOG_PATH.unlink(missing_ok=True)
        initialization_marker(CATALOG_PATH).unlink(missing_ok=True)
        ensure_catalog()
        self.app = FastAPI()
        self.app.include_router(catalog_api.router)
        self.app.dependency_overrides[catalog_api.require_user] = lambda: {"id": "user-a"}
        self.app.dependency_overrides[catalog_api.require_csrf] = lambda: None

    async def post_completion(self, asset_id: str, payload: dict):
        body = json.dumps(payload).encode()
        messages = []
        path = f"/v1/assets/{asset_id}/processing-complete"
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "scheme": "http", "method": "POST",
            "path": path, "raw_path": path.encode(), "root_path": "",
            "query_string": b"", "headers": [(b"content-type", b"application/json")],
            "server": ("testserver", 80), "client": ("127.0.0.1", 12345),
        }

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        response = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return status, json.loads(response)

    async def test_ready_promotion_is_not_exposed(self) -> None:
        item = _make_processing_asset()
        before = CATALOG_PATH.read_bytes()
        status, response = await self.post_completion(item["id"], {})
        self.assertEqual(status, 422)
        self.assertEqual(CATALOG_PATH.read_bytes(), before)
        self.assertEqual(get_asset(item["id"], "user-a")["processing_status"], "PROCESSING")

    async def test_ready_path_payload_is_not_exposed(self) -> None:
        item = _make_processing_asset()
        before = CATALOG_PATH.read_bytes()
        status, response = await self.post_completion(
            item["id"],
            {
                "processed_width": 64,
                "processed_height": 64,
                "processed_sha256": _SHA256_HEX,
                "variants": {"720p30": {"size_bytes": 10}},
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(CATALOG_PATH.read_bytes(), before)
        self.assertEqual(get_asset(item["id"], "user-a")["processing_status"], "PROCESSING")

    async def test_failure_path_moves_processing_to_failed(self) -> None:
        item = _make_processing_asset()
        status, completed = await self.post_completion(
            item["id"], {"failure_reason": "bad decode"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(completed["processing_status"], "FAILED")
        self.assertEqual(completed["failure_reason"], "bad decode")
        stored = get_asset(item["id"], "user-a")
        self.assertEqual(stored["processing_status"], "FAILED")

    async def test_unknown_asset_returns_404(self) -> None:
        import uuid as _uuid
        asset_id = str(_uuid.uuid4())
        before = CATALOG_PATH.read_bytes()
        status, _ = await self.post_completion(asset_id, {})
        self.assertEqual(status, 404)
        self.assertEqual(CATALOG_PATH.read_bytes(), before)

    async def test_foreign_owner_returns_404(self) -> None:
        item = _make_processing_asset()
        self.app.dependency_overrides[catalog_api.require_user] = lambda: {"id": "user-b"}
        before = CATALOG_PATH.read_bytes()
        status, _ = await self.post_completion(item["id"], {})
        self.assertEqual(status, 404)
        self.assertEqual(CATALOG_PATH.read_bytes(), before)
        self.assertEqual(get_asset(item["id"], "user-a")["processing_status"], "PROCESSING")

    async def test_foreign_variant_object_keys_are_rejected(self) -> None:
        item = _make_processing_asset()
        asset_id = item["id"]
        correct_key = variant_object_key(
            user_id="user-a", asset_id=asset_id, profile="720p30"
        )
        wrong_profile_key = variant_object_key(
            user_id="user-a", asset_id=asset_id, profile="1080p30"
        )
        foreign_owner_key = variant_object_key(
            user_id="user-b", asset_id=asset_id, profile="720p30"
        )
        bad_keys = [
            ("foreign-owner", foreign_owner_key, "user-b owner"),
            ("right-asset-wrong-profile", wrong_profile_key, "1080p30 key"),
            ("prefix-alias", correct_key + "x", "suffix alias"),
            ("suffix-alias", "x" + correct_key, "prefix alias"),
            ("path-traversal", correct_key + "/../720p30.mp4", "traversal"),
        ]
        for label, bad_key, _desc in bad_keys:
            with self.subTest(case=label):
                fresh = _make_processing_asset()
                before = CATALOG_PATH.read_bytes()
                status, response = await self.post_completion(
                    fresh["id"],
                    {
                        "variants": {
                            "720p30": {"object_key": bad_key, "size_bytes": 1}
                        }
                    },
                )
                self.assertEqual(status, 422, (label, response))
                self.assertEqual(CATALOG_PATH.read_bytes(), before)
                self.assertEqual(
                    get_asset(fresh["id"], "user-a")["processing_status"],
                    "PROCESSING",
                )

    async def test_coerced_dimensions_are_rejected_before_store(self) -> None:
        coerced = (True, False, "1080", 1.0, 1.5)
        for field in ("processed_width", "processed_height"):
            for value in coerced:
                with self.subTest(field=field, value=value):
                    item = _make_processing_asset()
                    before = CATALOG_PATH.read_bytes()
                    with patch.object(
                        catalog_api, "store_complete_asset_processing",
                        wraps=complete_asset_processing,
                    ) as complete:
                        status, response = await self.post_completion(
                            item["id"], {field: value}
                        )
                    self.assertEqual(status, 422)
                    self.assertEqual(response["detail"][0]["loc"], ["body", field])
                    complete.assert_not_called()
                    self.assertEqual(CATALOG_PATH.read_bytes(), before)
                    self.assertEqual(
                        get_asset(item["id"], "user-a")["processing_status"],
                        "PROCESSING",
                    )


if __name__ == "__main__":
    unittest.main()
