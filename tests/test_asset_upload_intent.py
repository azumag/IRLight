from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

# Point the store at a throwaway STATE_DIR before importing catalog_store.
_TMP = tempfile.mkdtemp(prefix="irlight-asset-upload-intent-")
os.environ["STATE_DIR"] = _TMP

from asset_upload_intent import (  # noqa: E402
    MAX_UPLOAD_BYTES,
    AssetUploadIntentError,
    is_owner_bound_source_object_key,
    normalize_image_content_type,
    source_object_key,
    validate_upload_completion,
    variant_object_key,
)
from catalog_store import (  # noqa: E402
    CATALOG_PATH,
    CatalogNotFound,
    CatalogValidationError,
    complete_asset_upload,
    create_asset_upload_intent,
    ensure_catalog,
    get_asset,
    create_asset,
)
from state_safety import initialization_marker  # noqa: E402

import catalog_api  # noqa: E402

_SHA256_HEX = "a" * 64


class AssetUploadIntentModuleTest(unittest.TestCase):
    def test_source_key_matches_documented_layout(self) -> None:
        asset_id = str(uuid.uuid4())
        self.assertEqual(
            source_object_key(user_id="user-a", asset_id=asset_id),
            f"users/user-a/assets/{asset_id}/source",
        )

    def test_source_key_rejects_unusable_segments(self) -> None:
        for value in ("", "/", "../evil", "a/b", "user\\b", ".", "..", "user.", "a b", "x" * 201, 123):
            with self.subTest(value=value):
                with self.assertRaises(AssetUploadIntentError):
                    source_object_key(user_id=value, asset_id="asset-1")
        with self.assertRaises(AssetUploadIntentError):
            source_object_key(user_id="user-a", asset_id="asset/1")

    def test_variant_key_uses_supported_profiles_only(self) -> None:
        asset_id = "asset-1"
        self.assertEqual(
            variant_object_key(user_id="user-a", asset_id=asset_id, profile="720p30"),
            f"users/user-a/assets/{asset_id}/variants/720p30.mp4",
        )
        self.assertEqual(
            variant_object_key(user_id="user-a", asset_id=asset_id, profile="1080p30"),
            f"users/user-a/assets/{asset_id}/variants/1080p30.mp4",
        )
        for profile in ("", "720p", "1080p60", "../720p30", None):
            with self.subTest(profile=profile):
                with self.assertRaises(AssetUploadIntentError):
                    variant_object_key(user_id="user-a", asset_id=asset_id, profile=profile)

    def test_normalize_content_type_accepts_parameters_and_case(self) -> None:
        self.assertEqual(normalize_image_content_type("image/png"), "image/png")
        self.assertEqual(normalize_image_content_type("IMAGE/JPEG"), "image/jpeg")
        self.assertEqual(
            normalize_image_content_type("image/webp; charset=binary"), "image/webp"
        )

    def test_normalize_content_type_rejects_svg_and_unknown(self) -> None:
        for value in (
            "image/svg+xml",
            "application/octet-stream",
            "text/html",
            "",
            "image/png" + " " * 300,
            None,
        ):
            with self.subTest(value=value):
                with self.assertRaises(AssetUploadIntentError):
                    normalize_image_content_type(value)

    def test_owner_bound_predicate_accepts_only_exact_key(self) -> None:
        asset_id = "asset-1"
        exact = source_object_key(user_id="user-a", asset_id=asset_id)
        self.assertTrue(
            is_owner_bound_source_object_key(
                user_id="user-a", asset_id=asset_id, object_key=exact
            )
        )
        for key in (
            "",
            exact + " ",
            exact + "/extra",
            f"users/user-b/assets/{asset_id}/source",
            f"users/user-a/assets/other/source",
            "users/user-a/assets/asset-1/sourcee",
            None,
            123,
        ):
            with self.subTest(key=key):
                self.assertFalse(
                    is_owner_bound_source_object_key(
                        user_id="user-a", asset_id=asset_id, object_key=key
                    )
                )

    def test_validate_completion_normalizes_and_rejects_bad_inputs(self) -> None:
        asset_id = "asset-1"
        key = source_object_key(user_id="user-a", asset_id=asset_id)
        completion = validate_upload_completion(
            user_id="user-a",
            asset_id=asset_id,
            content_type="image/png; charset=binary",
            object_key=key,
            size_bytes=1024,
            sha256=_SHA256_HEX,
        )
        self.assertEqual(completion["object_key"], key)
        self.assertEqual(completion["content_type"], "image/png")
        self.assertEqual(completion["size_bytes"], 1024)
        self.assertEqual(completion["sha256"], _SHA256_HEX)

        bad_cases = (
            {"object_key": "users/other/assets/asset-1/source"},
            {"object_key": key + "/x"},
            {"size_bytes": 0},
            {"size_bytes": -1},
            {"size_bytes": MAX_UPLOAD_BYTES + 1},
            {"size_bytes": True},
            {"size_bytes": 1.5},
            {"sha256": "A" * 64},
            {"sha256": "a" * 63},
            {"sha256": "z" * 64},
            {"sha256": "a" * 65},
            {"content_type": "image/svg+xml"},
        )
        for override in bad_cases:
            with self.subTest(override=override):
                arguments = {
                    "user_id": "user-a",
                    "asset_id": asset_id,
                    "content_type": "image/png",
                    "object_key": key,
                    "size_bytes": 1024,
                    "sha256": _SHA256_HEX,
                }
                arguments.update(override)
                with self.assertRaises(AssetUploadIntentError):
                    validate_upload_completion(**arguments)


class AssetUploadIntentStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        if CATALOG_PATH.exists():
            CATALOG_PATH.unlink()
        initialization_marker(CATALOG_PATH).unlink(missing_ok=True)
        ensure_catalog()

    def test_intent_issues_server_generated_owner_bound_key(self) -> None:
        item = create_asset_upload_intent(user_id="user-a", content_type="image/png")
        self.assertEqual(item["processing_status"], "UPLOADING")
        self.assertEqual(item["content_type"], "image/png")
        self.assertEqual(item["max_bytes"], MAX_UPLOAD_BYTES)
        self.assertEqual(
            item["source_object_key"],
            source_object_key(user_id="user-a", asset_id=item["id"]),
        )
        fetched = get_asset(item["id"], "user-a")
        self.assertEqual(fetched["source_object_key"], item["source_object_key"])

    def test_intent_rejects_unsupported_media_type(self) -> None:
        with self.assertRaises(CatalogValidationError):
            create_asset_upload_intent(user_id="user-a", content_type="image/svg+xml")

    def test_intent_rejects_traversing_owner_id(self) -> None:
        with self.assertRaises(CatalogValidationError):
            create_asset_upload_intent(user_id="../user-b", content_type="image/png")

    def test_completion_advances_uploading_asset_to_processing(self) -> None:
        item = create_asset_upload_intent(user_id="user-a", content_type="image/webp")
        completed = complete_asset_upload(
            asset_id=item["id"],
            user_id="user-a",
            content_type="image/webp",
            object_key=item["source_object_key"],
            size_bytes=2048,
            sha256=_SHA256_HEX,
        )
        self.assertEqual(completed["processing_status"], "PROCESSING")
        self.assertEqual(completed["uploaded_size_bytes"], 2048)
        self.assertEqual(completed["uploaded_sha256"], _SHA256_HEX)

    def test_completion_rejects_foreign_object_key(self) -> None:
        item = create_asset_upload_intent(user_id="user-a", content_type="image/png")
        with self.assertRaises(CatalogValidationError):
            complete_asset_upload(
                asset_id=item["id"],
                user_id="user-a",
                content_type="image/png",
                object_key=f"users/user-b/assets/{item['id']}/source",
                size_bytes=2048,
                sha256=_SHA256_HEX,
            )

    def test_completion_rejects_content_type_drift(self) -> None:
        item = create_asset_upload_intent(user_id="user-a", content_type="image/png")
        with self.assertRaises(CatalogValidationError):
            complete_asset_upload(
                asset_id=item["id"],
                user_id="user-a",
                content_type="image/jpeg",
                object_key=item["source_object_key"],
                size_bytes=2048,
                sha256=_SHA256_HEX,
            )

    def test_completion_is_single_use(self) -> None:
        item = create_asset_upload_intent(user_id="user-a", content_type="image/png")
        complete_asset_upload(
            asset_id=item["id"],
            user_id="user-a",
            content_type="image/png",
            object_key=item["source_object_key"],
            size_bytes=2048,
            sha256=_SHA256_HEX,
        )
        with self.assertRaises(CatalogValidationError):
            complete_asset_upload(
                asset_id=item["id"],
                user_id="user-a",
                content_type="image/png",
                object_key=item["source_object_key"],
                size_bytes=2048,
                sha256=_SHA256_HEX,
            )

    def test_completion_rejects_other_owner_and_unknown_asset(self) -> None:
        item = create_asset_upload_intent(user_id="user-a", content_type="image/png")
        with self.assertRaises(CatalogNotFound):
            complete_asset_upload(
                asset_id=item["id"],
                user_id="user-b",
                content_type="image/png",
                object_key=item["source_object_key"],
                size_bytes=2048,
                sha256=_SHA256_HEX,
            )
        with self.assertRaises(CatalogNotFound):
            complete_asset_upload(
                asset_id="missing",
                user_id="user-a",
                content_type="image/png",
                object_key="users/user-a/assets/missing/source",
                size_bytes=2048,
                sha256=_SHA256_HEX,
            )

    def test_completion_rejects_tampered_stored_key(self) -> None:
        item = create_asset_upload_intent(user_id="user-a", content_type="image/png")
        # A persisted record whose key was rewritten to another owner must not
        # be completable even when the caller echoes the correct derived key.
        with CATALOG_PATH.open("r", encoding="utf-8") as handle:
            catalog = json.load(handle)
        catalog["assets"][item["id"]]["source_object_key"] = (
            f"users/user-b/assets/{item['id']}/source"
        )
        with CATALOG_PATH.open("w", encoding="utf-8") as handle:
            json.dump(catalog, handle)
        with self.assertRaises(CatalogValidationError):
            complete_asset_upload(
                asset_id=item["id"],
                user_id="user-a",
                content_type="image/png",
                object_key=item["source_object_key"],
                size_bytes=2048,
                sha256=_SHA256_HEX,
            )

    def test_legacy_create_asset_is_unbound_and_unchanged(self) -> None:
        # The legacy metadata-only path keeps its historical shape; it does not
        # grant any owner-bound upload authority.
        legacy = create_asset(user_id="user-a", source_object_key="uploads/standby.png")
        self.assertEqual(legacy["processing_status"], "PENDING")
        self.assertEqual(legacy["source_object_key"], "uploads/standby.png")


class AssetUploadIntentApiTest(unittest.TestCase):
    def setUp(self) -> None:
        if CATALOG_PATH.exists():
            CATALOG_PATH.unlink()
        initialization_marker(CATALOG_PATH).unlink(missing_ok=True)
        ensure_catalog()

    def test_api_intent_returns_owner_bound_key(self) -> None:
        request = catalog_api.AssetUploadIntentCreate(content_type="image/png")
        issued = catalog_api.create_asset_upload_intent(
            request, {"id": "user-a"}, _csrf=None
        )
        self.assertEqual(
            issued["source_object_key"],
            source_object_key(user_id="user-a", asset_id=issued["id"]),
        )

    def test_api_intent_maps_unsupported_type_to_422(self) -> None:
        request = catalog_api.AssetUploadIntentCreate(content_type="image/svg+xml")
        with self.assertRaises(HTTPException) as raised:
            catalog_api.create_asset_upload_intent(request, {"id": "user-a"}, _csrf=None)
        self.assertEqual(raised.exception.status_code, 422)

    def test_api_completion_round_trip(self) -> None:
        issued = catalog_api.create_asset_upload_intent(
            catalog_api.AssetUploadIntentCreate(content_type="image/jpeg"),
            {"id": "user-a"},
            _csrf=None,
        )
        completion = catalog_api.AssetUploadCompletion(
            content_type="image/jpeg",
            object_key=issued["source_object_key"],
            size_bytes=4096,
            sha256=_SHA256_HEX,
        )
        completed = catalog_api.complete_asset_upload(
            issued["id"], completion, {"id": "user-a"}, _csrf=None
        )
        self.assertEqual(completed["processing_status"], "PROCESSING")

    def test_api_completion_maps_unknown_asset_to_404(self) -> None:
        completion = catalog_api.AssetUploadCompletion(
            content_type="image/png",
            object_key="users/user-a/assets/missing/source",
            size_bytes=4096,
            sha256=_SHA256_HEX,
        )
        with self.assertRaises(HTTPException) as raised:
            catalog_api.complete_asset_upload(
                "missing", completion, {"id": "user-a"}, _csrf=None
            )
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
