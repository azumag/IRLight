from __future__ import annotations

import json
import logging
import os
import random
import signal
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402


LOG = logging.getLogger("irlight.egress")


TERMINAL_REASON_CODES = {"AUTH_FAILED", "PUBLISH_CONFLICT", "LOCAL_PIPELINE_FAILED"}
ALLOWED_STATUSES = {
    "STARTING",
    "CONNECTED",
    "RECONNECTING",
    "AUTH_FAILED",
    "FAILED",
    "STOPPED",
}


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None else float(raw)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None else int(raw)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_destination_url(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("egress destination secret file is unavailable") from exc
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"rtmp", "rtmps"} or not parsed.hostname:
        raise RuntimeError("egress destination URL is invalid")
    return value


def safe_destination(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), parsed.hostname or ""


def classify_error(
    *,
    source_name: str,
    message: str,
    debug: str | None = None,
) -> str:
    """Map GStreamer/librtmp errors to stable, secret-safe reason codes.

    The raw message/debug text may contain the destination URL or stream key,
    so callers must not persist or log those strings.
    """
    if source_name.startswith("src") or "rtspsrc" in source_name:
        return "UPSTREAM_UNAVAILABLE"

    haystack = f"{message}\n{debug or ''}".lower()
    if any(token in haystack for token in ("unauthorized", "forbidden", "auth failed", "authfailed", "401", "403", "invalid stream key")):
        return "AUTH_FAILED"
    if any(token in haystack for token in ("badname", "already publishing", "publish conflict", "stream already")):
        return "PUBLISH_CONFLICT"
    if any(token in haystack for token in ("certificate", "tls", "ssl")):
        return "TLS_FAILED"
    if any(token in haystack for token in ("could not resolve", "name or service not known", "temporary failure in name resolution", "dns")):
        return "DNS_FAILED"
    if "timeout" in haystack or "timed out" in haystack:
        return "TIMEOUT"
    if any(token in haystack for token in ("connection refused", "network is unreachable", "no route to host", "could not connect", "connection reset")):
        return "UNREACHABLE"
    return "EGRESS_PIPELINE_FAILED"


@dataclass(frozen=True)
class ReconnectPolicy:
    initial_seconds: float = 1.0
    max_seconds: float = 30.0
    multiplier: float = 2.0
    jitter_ratio: float = 0.2
    max_attempts: int = 0
    max_elapsed_seconds: float = 0.0

    def delay_for(self, failure_count: int, random_value: float = 0.5) -> float:
        exponent = max(0, failure_count - 1)
        base = min(self.max_seconds, self.initial_seconds * (self.multiplier**exponent))
        bounded_random = min(1.0, max(0.0, random_value))
        factor = 1.0 + ((bounded_random * 2.0) - 1.0) * self.jitter_ratio
        return max(0.0, base * factor)

    def exhausted(self, failure_count: int, elapsed_seconds: float) -> bool:
        if self.max_attempts > 0 and failure_count >= self.max_attempts:
            return True
        if self.max_elapsed_seconds > 0 and elapsed_seconds >= self.max_elapsed_seconds:
            return True
        return False


@dataclass
class AttemptResult:
    reason_code: str
    connected_once: bool
    rendered_buffers: int
    terminal: bool = False
    error_domain: str | None = None
    error_code: int | None = None


class EgressAttempt:
    def __init__(self, input_uri: str, destination_url: str, stop_event: threading.Event) -> None:
        self.input_uri = input_uri
        self.destination_url = destination_url
        self.stop_event = stop_event
        self.pipeline: Gst.Pipeline | None = None
        self.loop = GLib.MainLoop()
        self.sink: Gst.Element | None = None
        self.mux: Gst.Element | None = None
        self.connected_once = False
        self.rendered_buffers = 0
        self.result = AttemptResult("EGRESS_PIPELINE_FAILED", False, 0)
        self._requested_mux_pads: list[Gst.Pad] = []
        self.on_connected: callable | None = None

    @staticmethod
    def _make(factory: str, name: str) -> Gst.Element:
        element = Gst.ElementFactory.make(factory, name)
        if element is None:
            raise RuntimeError(f"required GStreamer element is unavailable: {factory}")
        return element

    def _build(self) -> None:
        pipeline = Gst.Pipeline.new("irlight-egress")
        source = self._make("rtspsrc", "src")
        mux = self._make("flvmux", "mux")
        sink = self._make("rtmpsink", "egress_sink")

        source.set_property("location", self.input_uri)
        source.set_property("protocols", 4)  # TCP
        source.set_property("latency", env_int("EGRESS_INPUT_LATENCY_MS", 500))
        source.set_property("tcp-timeout", env_int("EGRESS_INPUT_TCP_TIMEOUT_US", 3_000_000))
        mux.set_property("streamable", True)
        sink.set_property("location", self.destination_url)
        sink.set_property("sync", False)
        sink.set_property("async", False)

        pipeline.add(source)
        pipeline.add(mux)
        pipeline.add(sink)
        if not mux.link(sink):
            raise RuntimeError("failed to link FLV muxer to RTMP sink")
        source.connect("pad-added", self._on_source_pad)

        self.pipeline = pipeline
        self.sink = sink
        self.mux = mux

    def _on_source_pad(self, _source: Gst.Element, pad: Gst.Pad) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        structure = caps.get_structure(0) if caps and caps.get_size() else None
        if structure is None:
            return
        media = (structure.get_string("media") or "").lower()
        encoding = (structure.get_string("encoding-name") or "").upper()
        if media == "video" and encoding == "H264":
            self._attach_track(pad, "video")
        elif media == "audio" and encoding in {"MPEG4-GENERIC", "MP4A-LATM"}:
            self._attach_track(pad, "audio")

    def _attach_track(self, source_pad: Gst.Pad, kind: str) -> None:
        pipeline = self.pipeline
        mux = self.mux
        if pipeline is None or mux is None:
            return
        mux_pad = mux.get_static_pad(kind)
        if mux_pad is not None and mux_pad.is_linked():
            return
        mux_pad = mux.request_pad_simple(kind)
        if mux_pad is None:
            raise RuntimeError(f"cannot request flvmux {kind} pad")
        self._requested_mux_pads.append(mux_pad)

        if kind == "video":
            depay = self._make("rtph264depay", "video_depay")
            parser = self._make("h264parse", "video_parse")
            parser.set_property("config-interval", -1)
            capsfilter = self._make("capsfilter", "video_caps")
            capsfilter.set_property(
                "caps", Gst.Caps.from_string("video/x-h264,stream-format=avc,alignment=au")
            )
        else:
            depay = self._make("rtpmp4gdepay", "audio_depay")
            parser = self._make("aacparse", "audio_parse")
            capsfilter = self._make("capsfilter", "audio_caps")
            capsfilter.set_property(
                "caps", Gst.Caps.from_string("audio/mpeg,mpegversion=4,stream-format=raw")
            )
        queue = self._make("queue", f"{kind}_queue")

        for element in (depay, parser, capsfilter, queue):
            pipeline.add(element)
        if not depay.link(parser) or not parser.link(capsfilter) or not capsfilter.link(queue):
            raise RuntimeError(f"failed to link {kind} egress chain")
        queue_src = queue.get_static_pad("src")
        if queue_src is None or queue_src.link(mux_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"failed to link {kind} to flvmux")
        depay_sink = depay.get_static_pad("sink")
        if depay_sink is None or source_pad.link(depay_sink) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"failed to link RTSP {kind} pad")
        for element in (depay, parser, capsfilter, queue):
            element.sync_state_with_parent()

    def _poll_sink(self) -> bool:
        if self.stop_event.is_set():
            self.result = AttemptResult("STOPPED", self.connected_once, self.rendered_buffers)
            self.loop.quit()
            return False
        sink = self.sink
        if sink is None:
            return True
        try:
            stats = sink.get_property("stats")
            rendered = int(stats.get_value("rendered")) if stats is not None else 0
        except (TypeError, ValueError, AttributeError):
            rendered = self.rendered_buffers
        self.rendered_buffers = max(self.rendered_buffers, rendered)
        if rendered > 0 and not self.connected_once:
            self.connected_once = True
            if self.on_connected is not None:
                self.on_connected(rendered)
        return True

    def _on_bus_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            source_name = message.src.get_name() if message.src is not None else "unknown"
            reason = classify_error(
                source_name=source_name,
                message=str(getattr(error, "message", "")),
                debug=debug,
            )
            self.result = AttemptResult(
                reason,
                self.connected_once,
                self.rendered_buffers,
                terminal=reason in TERMINAL_REASON_CODES,
                error_domain=str(getattr(error, "domain", "")) or None,
                error_code=int(getattr(error, "code", 0)),
            )
            self.loop.quit()
        elif message.type == Gst.MessageType.EOS:
            self.result = AttemptResult("UPSTREAM_EOS", self.connected_once, self.rendered_buffers)
            self.loop.quit()

    def run(self, on_connected: callable | None = None) -> AttemptResult:
        self.on_connected = on_connected
        try:
            self._build()
        except Exception:
            LOG.exception("failed to build egress pipeline")
            return AttemptResult("LOCAL_PIPELINE_FAILED", False, 0, terminal=True)
        assert self.pipeline is not None
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)
        GLib.timeout_add(250, self._poll_sink)
        state_result = self.pipeline.set_state(Gst.State.PLAYING)
        if state_result == Gst.StateChangeReturn.FAILURE:
            self.pipeline.set_state(Gst.State.NULL)
            return AttemptResult("EGRESS_PIPELINE_FAILED", False, 0)
        try:
            self.loop.run()
        finally:
            self.pipeline.set_state(Gst.State.NULL)
            for pad in self._requested_mux_pads:
                if self.mux is not None:
                    self.mux.release_request_pad(pad)
        return self.result


class EgressGateway:
    def __init__(self) -> None:
        self.input_uri = os.getenv("EGRESS_INPUT_URI", "rtsp://mediamtx:8554/output/relay")
        self.destination_file = Path(
            os.getenv("EGRESS_URL_FILE", "/run/irlight/secrets/egress_url")
        )
        self.status_file = Path(os.getenv("EGRESS_STATUS_FILE", "/state/egress.json"))
        self.policy = ReconnectPolicy(
            initial_seconds=env_float("EGRESS_RETRY_INITIAL_SECONDS", 1.0),
            max_seconds=env_float("EGRESS_RETRY_MAX_SECONDS", 30.0),
            multiplier=env_float("EGRESS_RETRY_MULTIPLIER", 2.0),
            jitter_ratio=env_float("EGRESS_RETRY_JITTER_RATIO", 0.2),
            max_attempts=env_int("EGRESS_MAX_ATTEMPTS", 0),
            max_elapsed_seconds=env_float("EGRESS_MAX_RETRY_SECONDS", 0.0),
        )
        self.stop_event = threading.Event()
        self.started_at = time.time()
        self.failure_count = 0
        self.had_connection = False
        self.destination_scheme = ""
        self.destination_host = ""

    def request_stop(self, _signum: int, _frame: object) -> None:
        self.stop_event.set()

    def _write_status(
        self,
        status: str,
        *,
        connected: bool,
        reason_code: str | None = None,
        rendered_buffers: int = 0,
        next_retry_at: float | None = None,
        error_domain: str | None = None,
        error_code: int | None = None,
    ) -> None:
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"invalid egress status: {status}")
        atomic_write_json(
            self.status_file,
            {
                "status": status,
                "connected": connected,
                "attempt": self.failure_count + 1,
                "reason_code": reason_code,
                "rendered_buffers": rendered_buffers,
                "next_retry_at": next_retry_at,
                "error_domain": error_domain,
                "error_code": error_code,
                "destination_scheme": self.destination_scheme,
                "destination_host": self.destination_host,
                "started_at": self.started_at,
                "observed_at": time.time(),
            },
        )

    def run(self) -> int:
        Gst.init(None)
        try:
            destination_url = read_destination_url(self.destination_file)
        except RuntimeError:
            LOG.error("egress destination secret file is unavailable or invalid")
            self._write_status("FAILED", connected=False, reason_code="SECRET_UNAVAILABLE")
            return 1
        self.destination_scheme, self.destination_host = safe_destination(destination_url)
        self._write_status("STARTING", connected=False)
        monotonic_started = time.monotonic()

        while not self.stop_event.is_set():
            attempt = EgressAttempt(self.input_uri, destination_url, self.stop_event)

            def connected(rendered: int) -> None:
                self.had_connection = True
                self.failure_count = 0
                self._write_status("CONNECTED", connected=True, rendered_buffers=rendered)

            result = attempt.run(on_connected=connected)
            if self.stop_event.is_set() or result.reason_code == "STOPPED":
                self._write_status("STOPPED", connected=False, reason_code="USER_STOPPED", rendered_buffers=result.rendered_buffers)
                return 0

            self.failure_count += 1
            if result.terminal:
                status = "AUTH_FAILED" if result.reason_code in {"AUTH_FAILED", "PUBLISH_CONFLICT"} else "FAILED"
                self._write_status(
                    status,
                    connected=False,
                    reason_code=result.reason_code,
                    rendered_buffers=result.rendered_buffers,
                    error_domain=result.error_domain,
                    error_code=result.error_code,
                )
                return 2

            elapsed = time.monotonic() - monotonic_started
            if self.policy.exhausted(self.failure_count, elapsed):
                self._write_status(
                    "FAILED",
                    connected=False,
                    reason_code="RETRY_EXHAUSTED",
                    rendered_buffers=result.rendered_buffers,
                    error_domain=result.error_domain,
                    error_code=result.error_code,
                )
                return 3

            delay = self.policy.delay_for(self.failure_count, random.random())
            self._write_status(
                "RECONNECTING",
                connected=False,
                reason_code=result.reason_code,
                rendered_buffers=result.rendered_buffers,
                next_retry_at=time.time() + delay,
                error_domain=result.error_domain,
                error_code=result.error_code,
            )
            if self.stop_event.wait(delay):
                break

        self._write_status("STOPPED", connected=False, reason_code="USER_STOPPED")
        return 0


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    gateway = EgressGateway()
    signal.signal(signal.SIGTERM, gateway.request_stop)
    signal.signal(signal.SIGINT, gateway.request_stop)
    return gateway.run()


if __name__ == "__main__":
    raise SystemExit(main())
