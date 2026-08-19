from __future__ import annotations

import binascii
import struct
import sys
import zlib
from pathlib import Path


def _chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind)
    checksum = binascii.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def write_png(path: Path, *, width: int = 1280, height: int = 720) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A neutral dark frame keeps the fallback deterministic and independent of
    # fonts, locale, network access, or external image tooling.
    pixel = bytes((16, 16, 16))
    row = b"\x00" + pixel * width
    raw = row * height
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: make_default_standby.py OUTPUT.png")
    destination = Path(sys.argv[1])
    write_png(destination)
    destination.chmod(0o444)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
