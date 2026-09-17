from __future__ import annotations

import binascii
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_HEADER_BYTES = 1024 * 1024
MAX_IMAGE_PIXELS = 16 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384
NODE_DEFAULT_IMAGE_PATH = "/opt/irlight/assets/default-standby.png"
_COPY_CHUNK_BYTES = 64 * 1024


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
        """Release the private standby snapshot without closing a reused fd."""

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


def _close_fd_quietly(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return the fields that must remain stable while source bytes are copied."""

    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("standby snapshot write made no progress")
        view = view[written:]


def _snapshot_regular_file_prefix(
    path: Path,
) -> tuple[bytes, int, tuple[int, int], int] | None:
    """Copy one stable regular file into a private, bounded decoder snapshot.

    Standby paths are a trusted Node-local handoff, but they can still be
    misconfigured to a symlink/FIFO/device, be replaced while Continuity checks
    them, or be modified in place by another writer after validation. Copy at
    most ``MAX_IMAGE_BYTES`` into a mode-0400 temporary file, verify that the
    source identity/content metadata stayed stable for the copy, then unlink the
    temporary pathname. The returned read-only fd is the only decoder handoff,
    so later writes to the source inode cannot change the selected bytes.
    """

    try:
        before = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(before.st_mode):
        return None

    source_flags = os.O_RDONLY
    source_flags |= getattr(os, "O_CLOEXEC", 0)
    source_flags |= getattr(os, "O_NONBLOCK", 0)
    source_flags |= getattr(os, "O_NOFOLLOW", 0)

    try:
        source_fd = os.open(path, source_flags)
    except OSError:
        return None

    snapshot_write_fd: int | None = None
    snapshot_read_fd: int | None = None
    snapshot_path: str | None = None
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("standby asset is not a regular file")
        if opened.st_size <= 0 or opened.st_size > MAX_IMAGE_BYTES:
            raise ValueError("standby asset size is outside the allowed range")

        after_open = os.lstat(path)
        if not stat.S_ISREG(after_open.st_mode):
            raise ValueError("standby asset path stopped being a regular file")
        source_identity = (opened.st_dev, opened.st_ino)
        if (after_open.st_dev, after_open.st_ino) != source_identity:
            raise ValueError("standby asset path changed during validation")
        source_signature = _stat_signature(opened)

        snapshot_write_fd, snapshot_path = tempfile.mkstemp(
            prefix="irlight-standby-", suffix=".image"
        )
        remaining = opened.st_size
        prefix_remaining = min(opened.st_size, MAX_IMAGE_HEADER_BYTES)
        prefix_parts: list[bytes] = []
        while remaining > 0:
            chunk = os.read(source_fd, min(_COPY_CHUNK_BYTES, remaining))
            if not chunk:
                raise ValueError("standby asset changed while being snapshotted")
            if prefix_remaining > 0:
                prefix_chunk = chunk[:prefix_remaining]
                prefix_parts.append(prefix_chunk)
                prefix_remaining -= len(prefix_chunk)
            _write_all(snapshot_write_fd, chunk)
            remaining -= len(chunk)

        after_copy = os.fstat(source_fd)
        if _stat_signature(after_copy) != source_signature:
            raise ValueError("standby asset changed while being snapshotted")
        after_path = os.lstat(path)
        if not stat.S_ISREG(after_path.st_mode):
            raise ValueError("standby asset path stopped being a regular file")
        if (after_path.st_dev, after_path.st_ino) != source_identity:
            raise ValueError("standby asset path changed while being snapshotted")

        snapshot_written = os.fstat(snapshot_write_fd)
        if not stat.S_ISREG(snapshot_written.st_mode):
            raise ValueError("standby snapshot is not a regular file")
        if snapshot_written.st_size != opened.st_size:
            raise ValueError("standby snapshot size does not match source")
        os.fchmod(snapshot_write_fd, stat.S_IRUSR)
        os.fsync(snapshot_write_fd)

        snapshot_flags = os.O_RDONLY
        snapshot_flags |= getattr(os, "O_CLOEXEC", 0)
        snapshot_flags |= getattr(os, "O_NOFOLLOW", 0)
        snapshot_read_fd = os.open(snapshot_path, snapshot_flags)
        snapshot_opened = os.fstat(snapshot_read_fd)
        snapshot_identity = (snapshot_opened.st_dev, snapshot_opened.st_ino)
        if not stat.S_ISREG(snapshot_opened.st_mode):
            raise ValueError("standby snapshot stopped being a regular file")
        if snapshot_opened.st_size != opened.st_size:
            raise ValueError("standby snapshot changed before decoder handoff")
        if snapshot_identity != (
            snapshot_written.st_dev,
            snapshot_written.st_ino,
        ):
            raise ValueError("standby snapshot path changed before decoder handoff")

        os.unlink(snapshot_path)
        snapshot_path = None
        _close_fd_quietly(snapshot_write_fd)
        snapshot_write_fd = None
        _close_fd_quietly(source_fd)
        source_fd = None
        os.lseek(snapshot_read_fd, 0, os.SEEK_SET)
        retained_fd = snapshot_read_fd
        snapshot_read_fd = None
        return b"".join(prefix_parts), retained_fd, snapshot_identity, opened.st_size
    except (OSError, ValueError):
        return None
    finally:
        _close_fd_quietly(source_fd)
        _close_fd_quietly(snapshot_write_fd)
        _close_fd_quietly(snapshot_read_fd)
        if snapshot_path is not None:
            try:
                os.unlink(snapshot_path)
            except OSError:
                pass


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
    # sees the private immutable snapshot even if the source pathname/inode is
    # later replaced or modified. /proc is the production Linux path; /dev/fd
    # keeps local Unix development environments working where available.
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
    opened = _snapshot_regular_file_prefix(path)
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
    symlinked, unsupported, malformed-header, excessive-dimension, acquisition
    races, and unusable decoder handoff inputs. A successful selection owns a
    private unlinked snapshot fd until the Continuity pipeline releases it, so
    later source-path replacement or in-place mutation cannot change decoder
    bytes. Deep decode/decompression validation still belongs to Issue #7.
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
