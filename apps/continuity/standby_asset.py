from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MAX_IMAGE_BYTES = 32 * 1024 * 1024
NODE_DEFAULT_IMAGE_PATH = "/opt/irlight/assets/default-standby.png"


@dataclass(frozen=True)
class StandbyAssetSelection:
    source: str
    path: Path | None
    fallback_reason: str | None
    custom_configured: bool


def _is_supported_image(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        size = path.stat().st_size
        if size <= 0 or size > MAX_IMAGE_BYTES:
            return False
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        return False

    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if header.startswith(b"\xff\xd8\xff"):
        return True
    return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"


def resolve_standby_asset(
    custom_path: str | None,
    fallback_path: str | None = NODE_DEFAULT_IMAGE_PATH,
) -> StandbyAssetSelection:
    """Choose a trusted local standby image without exposing filesystem paths.

    ``custom_path`` is expected to be a Node-prefetched, already validated image.
    These cheap local checks cover missing, empty, oversized, and obviously
    unsupported handoffs; deep decode/content validation belongs to Issue #7.
    """

    custom = (custom_path or "").strip()
    fallback = (fallback_path or "").strip()
    custom_configured = bool(custom)

    if custom:
        candidate = Path(custom)
        if _is_supported_image(candidate):
            return StandbyAssetSelection(
                source="CUSTOM",
                path=candidate,
                fallback_reason=None,
                custom_configured=True,
            )

    if fallback:
        candidate = Path(fallback)
        if _is_supported_image(candidate):
            return StandbyAssetSelection(
                source="NODE_DEFAULT",
                path=candidate,
                fallback_reason="ASSET_UNAVAILABLE" if custom_configured else None,
                custom_configured=custom_configured,
            )

    return StandbyAssetSelection(
        source="SYNTHETIC_BLACK",
        path=None,
        fallback_reason=(
            "ASSET_AND_NODE_DEFAULT_UNAVAILABLE"
            if custom_configured
            else "NODE_DEFAULT_UNAVAILABLE"
        ),
        custom_configured=custom_configured,
    )


def gst_standby_source(selection: StandbyAssetSelection) -> str:
    """Return only the source portion used before the existing raw-video caps."""

    if selection.path is None:
        return "videotestsrc name=standby_video is-live=true pattern=black !"

    uri = selection.path.resolve().as_uri()
    escaped = uri.replace("\\", "\\\\").replace('"', '\\"')
    return (
        f'uridecodebin name=standby_image_decode uri="{escaped}" ! '
        "imagefreeze ! videoconvert ! videoscale ! videorate !"
    )


def public_standby_status(selection: StandbyAssetSelection) -> dict[str, object]:
    """Safe diagnostics: never expose a local path or user-controlled filename."""

    return {
        "source": selection.source,
        "fallback_reason": selection.fallback_reason,
        "custom_configured": selection.custom_configured,
    }
