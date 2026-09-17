from __future__ import annotations

import binascii
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path


MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_HEADER_BYTES = 1024 * 1024
MAX_IMAGE_PIXELS = 16 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384
NODE_DEFAULT_IMAGE_PATH = "/opt/irlight/assets/default-standby.png"


@dataclass
class StandbyAssetSelection:
    source: str
    path: Path | None
    fallback_reason: str | None
    custom_configured: bool
    _pinned_fd: int | None = field(default=None, repr=False, compare=False)
    _pinned_identity: tuple[int, int] | None = field(
        default=None, repr=False, compare=False
    )

    def close(self) -> None:
        """Release the validated standby inode without closing a reused fd."""

        fd = self._pinned_fd
        identity = self._pinned_identity
        self._pinned_fd = None
        self._pinned_identity = None
        if fd is None:
            return

        try:
            opened = os.fstat(fd)
        except OSError:
            return
        if identity is not None and (opened.st_dev, opened.st_ino) != identity:
            return
        try:
            os.close(fd)
        except OSError:
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # Interpreter shutdown can tear down module globals before objects.
            pass


def _close_fd_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _open_regular_file_prefix(
    path: Path,
) -> tuple[bytes, int, tuple[int, int], int] | None:
    """Open one stable regular file and return a bounded prefix plus pinned fd.

    Standby paths are a trusted Node-local handoff, but they can still be
    misconfigured to a symlink/FIFO/device or be replaced while Continuity is
    checking them. Keep validation non-blocking and fail closed rather than
    following a link or opening a special file. The successful fd remains open
    so later decoder handoff can stay bound to the inode that was validated.
    """

    # Reject obvious special files before open. This is only a fast safety
    # check; the opened fd plus the post-open pathname identity check below are
    # authoritative, so inode reuse before open cannot make an old lstat result
    # look like proof for the file that was actually opened.
    try:
        before = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(before.st_mode):
        return None

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)

    try:
        fd = os.open(path, flags)
    except OSError:
        return None

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("standby asset is not a regular file")
        if opened.st_size <= 0 or opened.st_size > MAX_IMAGE_BYTES:
            raise ValueError("standby asset size is outside the allowed range")

        # Bind the validated pathname to the fd we actually opened. Keeping
        # that fd alive means its inode cannot be recycled while this check is
        # performed, so a path replacement after open is detected reliably.
        after = os.lstat(path)
        if not stat.S_ISREG(after.st_mode):
            raise ValueError("standby asset path stopped being a regular file")
        identity = (opened.st_dev, opened.st_ino)
        if (after.st_dev, after.st_ino) != identity:
            raise ValueError("standby asset path changed during validation")

        remaining = min(opened.st_size, MAX_IMAGE_HEADER_BYTES)
        chunks: list[bytes] = []
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        os.lseek(fd, 0, os.SEEK_SET)
        return b"".join(chunks), fd, identity, opened.st_size
    except (OSError, ValueError):
        _close_fd_quietly(fd)
        return None


def _png_dimensions(payload: bytes) -> tuple[int, int] | None:
    # A PNG signature plus the complete fixed-size IHDR chunk is 33 bytes.
    # Validate the chunk CRC and fields before trusting the declared dimensions.
    if len(payload) < 33 or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if payload[8:12] != b"\x00\x00\x00\r" or payload[12:16] != b"IHDR":
        return None

    ihdr = payload[16:29]
    expected_crc = int.from_bytes(payload[29:33], "big")
    actual_crc = binascii.crc32(b"IHDR")
    actual_crc = binascii.crc32(ihdr, actual_crc) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        return None

    bit_depth = ihdr[8]
    color_type = ihdr[9]
    compression = ihdr[10]
    filter_method = ihdr[11]
    interlace = ihdr[12]
    allowed_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    if bit_depth not in allowed_depths.get(color_type, set()):
        return None
    if compression != 0 or filter_method != 0 or interlace not in {0, 1}:
        return None

    return (
        int.from_bytes(ihdr[0:4], "big"),
        int.from_bytes(ihdr[4:8], "big"),
    )


_JPEG_SOF_MARKERS = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)


def _jpeg_dimensions(payload: bytes) -> tuple[int, int] | None:
    if len(payload) < 4 or not payload.startswith(b"\xff\xd8"):
        return None

    offset = 2
    while offset < len(payload):
        if payload[offset] != 0xFF:
            return None
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            return None

        marker = payload[offset]
        offset += 1
        if marker == 0x00:
            return None
        if marker == 0xD8:
            continue
        if marker in {0xD9, 0xDA}:
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue

        if offset + 2 > len(payload):
            return None
        segment_length = int.from_bytes(payload[offset : offset + 2], "big")
        if segment_length < 2:
            return None
        segment_end = offset + segment_length
        if segment_end > len(payload):
            return None

        if marker in _JPEG_SOF_MARKERS:
            if segment_length < 11:
                return None
            precision = payload[offset + 2]
            height = int.from_bytes(payload[offset + 3 : offset + 5], "big")
            width = int.from_bytes(payload[offset + 5 : offset + 7], "big")
            component_count = payload[offset + 7]
            if precision <= 0 or precision > 16 or component_count <= 0:
                return None
            if segment_length != 8 + 3 * component_count:
                return None
            return width, height

        offset = segment_end

    return None


def _webp_dimensions(payload: bytes, *, file_size: int) -> tuple[int, int] | None:
    if (
        len(payload) < 20
        or payload[:4] != b"RIFF"
        or payload[8:12] != b"WEBP"
    ):
        return None

    riff_size = int.from_bytes(payload[4:8], "little")
    if riff_size + 8 != file_size:
        return None

    chunk_type = payload[12:16]
    chunk_size = int.from_bytes(payload[16:20], "little")
    padded_chunk_size = chunk_size + (chunk_size & 1)
    if 20 + padded_chunk_size > file_size:
        return None

    if chunk_type == b"VP8X":
        if chunk_size != 10 or len(payload) < 30:
            return None
        width = 1 + int.from_bytes(payload[24:27], "little")
        height = 1 + int.from_bytes(payload[27:30], "little")
        return width, height

    if chunk_type == b"VP8L":
        if chunk_size < 5 or len(payload) < 25 or payload[20] != 0x2F:
            return None
        b1, b2, b3, b4 = payload[21:25]
        width = 1 + b1 + ((b2 & 0x3F) << 8)
        height = 1 + ((b2 & 0xC0) >> 6) + (b3 << 2) + ((b4 & 0x0F) << 10)
        return width, height

    if chunk_type == b"VP8 ":
        if (
            chunk_size < 10
            or len(payload) < 30
            or payload[23:26] != b"\x9d\x01\x2a"
        ):
            return None
        width = int.from_bytes(payload[26:28], "little") & 0x3FFF
        height = int.from_bytes(payload[28:30], "little") & 0x3FFF
        return width, height

    return None


def _supported_image_dimensions(
    payload: bytes, *, file_size: int
) -> tuple[int, int] | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png_dimensions(payload)
    if payload.startswith(b"\xff\xd8"):
        return _jpeg_dimensions(payload)
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return _webp_dimensions(payload, file_size=file_size)
    return None


def _dimensions_are_safe(dimensions: tuple[int, int]) -> bool:
    width, height = dimensions
    if width <= 0 or height <= 0:
        return False
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        return False
    return width <= MAX_IMAGE_PIXELS // height


def _pinned_fd_uri_for(fd: int, identity: tuple[int, int]) -> str | None:
    try:
        opened = os.fstat(fd)
    except OSError:
        return None
    if not stat.S_ISREG(opened.st_mode):
        return None
    if (opened.st_dev, opened.st_ino) != identity:
        return None

    # GStreamer opens a fresh descriptor through the process fd alias, so it
    # sees the already validated inode even if the original pathname is later
    # replaced. /proc is the production Linux path; /dev/fd keeps local Unix
    # development environments working where that alias is available instead.
    for base in (Path("/proc/self/fd"), Path("/dev/fd")):
        alias = base / str(fd)
        try:
            target = os.stat(alias)
        except OSError:
            continue
        if (target.st_dev, target.st_ino) == identity:
            return alias.as_uri()
    return None


def _open_supported_image(path: Path) -> tuple[int, tuple[int, int]] | None:
    opened = _open_regular_file_prefix(path)
    if opened is None:
        return None

    prefix, fd, identity, file_size = opened
    dimensions = _supported_image_dimensions(prefix, file_size=file_size)
    if dimensions is None or not _dimensions_are_safe(dimensions):
        _close_fd_quietly(fd)
        return None
    # Treat a decoder handoff that cannot be represented by a stable fd alias
    # as unavailable at selection time. This lets custom -> Node default ->
    # synthetic fallback and public diagnostics describe the source that can
    # actually be handed to GStreamer, rather than selecting a path that will
    # silently become black later during pipeline construction.
    if _pinned_fd_uri_for(fd, identity) is None:
        _close_fd_quietly(fd)
        return None
    return fd, identity


def resolve_standby_asset(
    custom_path: str | None,
    fallback_path: str | None = NODE_DEFAULT_IMAGE_PATH,
) -> StandbyAssetSelection:
    """Choose a trusted local standby image without exposing filesystem paths.

    ``custom_path`` is expected to be a Node-prefetched, already validated image.
    These cheap local checks cover missing, empty, oversized, non-regular,
    symlinked, unsupported, malformed-header, excessive-dimension, and unusable
    decoder-handoff inputs; deep decode/content validation belongs to Issue #7.
    A successful selection owns an open fd for the validated inode until the
    Continuity pipeline releases it.
    """

    custom = (custom_path or "").strip()
    fallback = (fallback_path or "").strip()
    custom_configured = bool(custom)

    if custom:
        candidate = Path(custom)
        opened = _open_supported_image(candidate)
        if opened is not None:
            fd, identity = opened
            return StandbyAssetSelection(
                source="CUSTOM",
                path=candidate,
                fallback_reason=None,
                custom_configured=True,
                _pinned_fd=fd,
                _pinned_identity=identity,
            )

    if fallback:
        candidate = Path(fallback)
        opened = _open_supported_image(candidate)
        if opened is not None:
            fd, identity = opened
            return StandbyAssetSelection(
                source="NODE_DEFAULT",
                path=candidate,
                fallback_reason="ASSET_UNAVAILABLE" if custom_configured else None,
                custom_configured=custom_configured,
                _pinned_fd=fd,
                _pinned_identity=identity,
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


def _pinned_fd_uri(selection: StandbyAssetSelection) -> str | None:
    fd = selection._pinned_fd
    identity = selection._pinned_identity
    if fd is None or identity is None:
        return None
    return _pinned_fd_uri_for(fd, identity)


def gst_standby_source(selection: StandbyAssetSelection) -> str:
    """Return only the source portion used before the existing raw-video caps."""

    if selection.path is None:
        return "videotestsrc name=standby_video is-live=true pattern=black !"

    uri = _pinned_fd_uri(selection)
    if uri is None:
        return "videotestsrc name=standby_video is-live=true pattern=black !"
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
