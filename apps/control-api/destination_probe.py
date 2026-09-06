"""Protocol-level reachability probes for streaming destinations.

RTMP/RTMPS probes perform the RTMP transport handshake without publishing
media. SRT probes use Haivision's ``srt-live-transmit`` CLI so a successful
result represents a real SRT handshake instead of a UDP-only reachability
guess.

Because destination URLs are user-controlled, probes reject non-public target
addresses by default to avoid turning the Control Plane into an SSRF primitive.
Private targets can be enabled explicitly for local/self-hosted deployments
with ``IRLIGHT_VERIFY_ALLOW_PRIVATE_TARGETS=1``.
"""

from __future__ import annotations

import ipaddress
import os
import queue
import secrets
import socket
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlsplit, urlunsplit

from destination_url_safety import (
    DestinationUrlSafetyError,
    validate_destination_url_secret_safety,
)


RTMP_HANDSHAKE_BYTES = 1536
RTMP_VERSION = 3
# Different srt-live-transmit releases use different wording for the same
# successful SRTS_CONNECTED state. Keep the legacy IRLight marker while also
# accepting the messages emitted by current upstream packages.
SRT_CONNECTED_MARKERS = (
    b"SRT target connected",
    b"Target connected (caller)",
    b"Target connected (listener)",
)


class DestinationProbeError(RuntimeError):
    """A destination could not be safely or successfully probed."""


@dataclass(frozen=True)
class ProbeConfig:
    timeout_seconds: float = 5.0
    allow_private_targets: bool = False
    srt_binary: str = "srt-live-transmit"

    @classmethod
    def from_env(cls) -> "ProbeConfig":
        raw_timeout = os.getenv("IRLIGHT_VERIFY_TIMEOUT_SECONDS", "5")
        try:
            timeout = float(raw_timeout)
        except ValueError:
            timeout = 5.0
        timeout = min(max(timeout, 0.5), 30.0)
        allow_private = os.getenv("IRLIGHT_VERIFY_ALLOW_PRIVATE_TARGETS", "") == "1"
        return cls(timeout_seconds=timeout, allow_private_targets=allow_private)


def probe_destination(url: str, config: ProbeConfig | None = None) -> dict[str, Any]:
    cfg = config or ProbeConfig.from_env()
    try:
        validate_destination_url_secret_safety(url)
    except DestinationUrlSafetyError as exc:
        raise DestinationProbeError(str(exc)) from exc

    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"rtmp", "rtmps", "srt"}:
        raise DestinationProbeError("unsupported destination URL scheme")
    if parsed.fragment:
        raise DestinationProbeError("destination URL fragments are not supported")
    if not parsed.hostname:
        raise DestinationProbeError("destination host is required")

    try:
        port = parsed.port
    except ValueError as exc:
        raise DestinationProbeError("invalid destination port") from exc

    if scheme == "srt":
        if port is None:
            raise DestinationProbeError("SRT destination port is required")
        return _probe_srt(parsed, port, cfg)

    port = port or (443 if scheme == "rtmps" else 1935)
    return _probe_rtmp(
        host=parsed.hostname,
        port=port,
        use_tls=scheme == "rtmps",
        config=cfg,
    )


def _resolve(
    host: str,
    port: int,
    *,
    socktype: int,
    allow_private_targets: bool,
) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
    try:
        addresses = socket.getaddrinfo(host, port, type=socktype)
    except socket.gaierror as exc:
        raise DestinationProbeError("destination hostname could not be resolved") from exc
    if not addresses:
        raise DestinationProbeError("destination hostname could not be resolved")

    safe: list[tuple[int, int, int, str, tuple[Any, ...]]] = []
    for entry in addresses:
        sockaddr = entry[4]
        ip_text = str(sockaddr[0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise DestinationProbeError("destination resolved to an invalid address") from exc

        if address.is_unspecified or address.is_multicast:
            raise DestinationProbeError("destination resolved to a disallowed address")
        if not allow_private_targets and not address.is_global:
            raise DestinationProbeError("destination must resolve to a public address")
        safe.append(entry)
    return safe


def _recv_exact(stream: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.recv(remaining)
        if not chunk:
            raise DestinationProbeError("RTMP peer closed during handshake")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _probe_rtmp(
    *,
    host: str,
    port: int,
    use_tls: bool,
    config: ProbeConfig,
) -> dict[str, Any]:
    addresses = _resolve(
        host,
        port,
        socktype=socket.SOCK_STREAM,
        allow_private_targets=config.allow_private_targets,
    )
    last_error: Exception | None = None
    started = time.monotonic()

    for family, socktype, proto, _canonname, sockaddr in addresses:
        raw: socket.socket | None = None
        stream: socket.socket | None = None
        try:
            raw = socket.socket(family, socktype, proto)
            raw.settimeout(config.timeout_seconds)
            raw.connect(sockaddr)
            stream = raw
            if use_tls:
                context = ssl.create_default_context()
                stream = context.wrap_socket(raw, server_hostname=host)
                stream.settimeout(config.timeout_seconds)

            timestamp = int(time.time()) & 0xFFFFFFFF
            c1 = (
                timestamp.to_bytes(4, "big")
                + b"\x00\x00\x00\x00"
                + secrets.token_bytes(RTMP_HANDSHAKE_BYTES - 8)
            )
            stream.sendall(bytes([RTMP_VERSION]) + c1)
            response = _recv_exact(stream, 1 + RTMP_HANDSHAKE_BYTES * 2)
            if response[0] != RTMP_VERSION:
                raise DestinationProbeError("peer did not complete an RTMP v3 handshake")
            # C2 echoes S1. We do not send RTMP commands or publish media.
            stream.sendall(response[1 : 1 + RTMP_HANDSHAKE_BYTES])
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            return {
                "protocol": "rtmps" if use_tls else "rtmp",
                "peer_ip": str(sockaddr[0]),
                "peer_port": port,
                "elapsed_ms": elapsed_ms,
            }
        except (OSError, ssl.SSLError, DestinationProbeError) as exc:
            last_error = exc
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            elif raw is not None:
                try:
                    raw.close()
                except OSError:
                    pass

    raise DestinationProbeError("destination did not complete the RTMP handshake") from last_error


def _read_stderr_lines(stream: Any, messages: queue.Queue[bytes | None]) -> None:
    """Forward child stderr lines without ever logging destination URI content."""

    try:
        while True:
            line = stream.readline()
            if not line:
                break
            messages.put(line)
    finally:
        messages.put(None)


def _probe_srt(parsed: Any, port: int, config: ProbeConfig) -> dict[str, Any]:
    query = parse_qsl(parsed.query, keep_blank_values=True)
    mode = next((value.lower() for key, value in query if key.lower() == "mode"), None)
    if mode is not None and mode != "caller":
        raise DestinationProbeError("SRT destination verification requires caller mode")

    addresses = _resolve(
        parsed.hostname,
        port,
        socktype=socket.SOCK_DGRAM,
        allow_private_targets=config.allow_private_targets,
    )
    # Resolve once, validate all answers, then hand the CLI a literal IP so a
    # second DNS lookup cannot redirect the probe to an internal address.
    sockaddr = addresses[0][4]
    peer_ip = str(sockaddr[0])
    host_for_uri = f"[{peer_ip}]" if ":" in peer_ip else peer_ip

    # Preserve the caller's original query encoding (notably streamid syntax)
    # while taking control of connection mode and timeout. Secret-bearing
    # streamids have already been rejected before this argv is constructed.
    raw_tokens: list[str] = []
    for token in parsed.query.split("&"):
        if not token:
            continue
        raw_key = token.split("=", 1)[0]
        key = unquote_plus(raw_key).lower()
        if key not in {"mode", "conntimeo"}:
            raw_tokens.append(token)
    raw_tokens.extend(
        [
            "mode=caller",
            f"conntimeo={int(config.timeout_seconds * 1000)}",
        ]
    )
    safe_uri = urlunsplit(
        (
            "srt",
            f"{host_for_uri}:{port}",
            parsed.path,
            "&".join(raw_tokens),
            "",
        )
    )

    # srt-live-transmit is a stream relay/sample app, so its final exit code
    # describes the entire stream lifecycle, not just the SRT handshake. Keep
    # stdin open and wait for the application's explicit SRTS_CONNECTED event.
    # This lets verify stop immediately after the transport handshake without
    # having to publish media or depend on the destination's application path.
    started = time.monotonic()
    connected_at: float | None = None
    process: subprocess.Popen[bytes] | None = None
    reader: threading.Thread | None = None
    messages: queue.Queue[bytes | None] = queue.Queue()
    try:
        process = subprocess.Popen(
            [
                config.srt_binary,
                "file://con",
                safe_uri,
                "-loglevel:error",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if process.stderr is None:
            raise DestinationProbeError("SRT verifier stderr is unavailable")

        reader = threading.Thread(
            target=_read_stderr_lines,
            args=(process.stderr, messages),
            daemon=True,
        )
        reader.start()
        deadline = started + config.timeout_seconds + 1.0

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DestinationProbeError("SRT destination handshake timed out")
            try:
                line = messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise DestinationProbeError("SRT destination handshake timed out") from exc

            if line is None:
                raise DestinationProbeError("destination did not complete the SRT handshake")
            if any(marker in line for marker in SRT_CONNECTED_MARKERS):
                connected_at = time.monotonic()
                break
    except FileNotFoundError as exc:
        raise DestinationProbeError("SRT verifier is unavailable") from exc
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
            if reader is not None:
                reader.join(timeout=1.0)
            if process.stdin is not None:
                process.stdin.close()
            if process.stderr is not None:
                process.stderr.close()

    if connected_at is None:
        raise DestinationProbeError("destination did not complete the SRT handshake")

    elapsed_ms = round((connected_at - started) * 1000, 1)
    return {
        "protocol": "srt",
        "peer_ip": peer_ip,
        "peer_port": port,
        "elapsed_ms": elapsed_ms,
    }
