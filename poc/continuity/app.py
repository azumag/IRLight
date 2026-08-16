from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from state import (  # noqa: E402
    AudioActual,
    AudioMode,
    RuntimeState,
    VersionConflictError,
    utc_now_iso,
)


LOGGER = logging.getLogger("irlight.continuity")
STATIC_INDEX = Path(__file__).with_name("index.html")


@dataclass(frozen=True)
class Settings:
    input_uri: str
    output_url: str
    public_ingest_url: str
    public_preview_url: str
    http_host: str
    http_port: int
    width: int
    height: int
    fps: int
    video_bitrate_kbps: int
    recovery_stable_seconds: float
    input_stale_seconds: float
    restart_delay_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            input_uri=os.getenv("IRLIGHT_INPUT_URI", "rtsp://mediamtx:8554/live/input"),
            output_url=os.getenv(
                "IRLIGHT_OUTPUT_URL",
                "rtmp://mediamtx:1935/output/stream",
            ),
            public_ingest_url=os.getenv(
                "IRLIGHT_PUBLIC_INGEST_URL",
                "rtmp://localhost:1935/live/input",
            ),
            public_preview_url=os.getenv(
                "IRLIGHT_PUBLIC_PREVIEW_URL",
                "rtmp://localhost:1935/output/stream",
            ),
            http_host=os.getenv("IRLIGHT_HTTP_HOST", "0.0.0.0"),
            http_port=_env_int("IRLIGHT_HTTP_PORT", 8080, minimum=1, maximum=65535),
            width=_env_int("IRLIGHT_WIDTH", 1280, minimum=320, maximum=3840),
            height=_env_int("IRLIGHT_HEIGHT", 720, minimum=180, maximum=2160),
            fps=_env_int("IRLIGHT_FPS", 30, minimum=1, maximum=60),
            video_bitrate_kbps=_env_int(
                "IRLIGHT_VIDEO_BITRATE_KBPS",
                3500,
                minimum=250,
                maximum=20000,
            ),
            recovery_stable_seconds=_env_float(
                "IRLIGHT_RECOVERY_STABLE_SECONDS",
                3.0,
                minimum=0.25,
                maximum=30.0,
            ),
            input_stale_seconds=_env_float(
                "IRLIGHT_INPUT_STALE_SECONDS",
                1.5,
                minimum=0.25,
                maximum=30.0,
            ),
            restart_delay_seconds=_env_float(
                "IRLIGHT_RESTART_DELAY_SECONDS",
                2.0,
                minimum=0.25,
                maximum=60.0,
            ),
        )


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    value = default if raw is None else int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    value = default if raw is None else float(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _gst_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _element_chain() -> tuple[str, str]:
    if Gst.ElementFactory.find("avenc_aac"):
        audio_encoder = "avenc_aac bitrate=128000"
    elif Gst.ElementFactory.find("voaacenc"):
        audio_encoder = "voaacenc bitrate=128000"
    else:
        raise RuntimeError("No AAC encoder found (avenc_aac or voaacenc is required)")

    if Gst.ElementFactory.find("rtmp2sink"):
        rtmp_sink = "rtmp2sink"
    elif Gst.ElementFactory.find("rtmpsink"):
        rtmp_sink = "rtmpsink"
    else:
        raise RuntimeError("No RTMP sink found (rtmp2sink or rtmpsink is required)")

    return audio_encoder, rtmp_sink


def _gerror_identity(error: GLib.Error) -> str:
    """Return a log-safe error identity without URI-bearing error text."""

    domain = getattr(error, "domain", "unknown")
    code = getattr(error, "code", "unknown")
    return f"{domain}:{code}"


class ContinuityEngine:
    """Single-session GStreamer continuity pipeline for the Phase 0 vertical slice.

    This baseline intentionally decodes and re-encodes video. It validates the
    always-running output, standby cutover, reconnect, and audio control model.
    A later PoC will compare this against H.264 passthrough variants.
    """

    def __init__(self, settings: Settings, state: RuntimeState) -> None:
        self.settings = settings
        self.state = state
        self._stop_event = threading.Event()
        self._input_failed = threading.Event()
        self._output_failed = threading.Event()
        self._pipeline_lock = threading.RLock()
        self._frame_lock = threading.RLock()

        self._input_pipeline: Gst.Pipeline | None = None
        self._output_pipeline: Gst.Pipeline | None = None
        self._video_selector: Gst.Element | None = None
        self._audio_selector: Gst.Element | None = None
        self._video_standby_pad: Gst.Pad | None = None
        self._video_live_pad: Gst.Pad | None = None
        self._audio_silence_pad: Gst.Pad | None = None
        self._audio_live_pad: Gst.Pad | None = None

        self._last_video_mono: float | None = None
        self._last_audio_mono: float | None = None
        self._last_video_iso: str | None = None
        self._last_audio_iso: str | None = None
        self._stable_since: float | None = None
        self._ever_received_input = False
        self._applied_video_live: bool | None = None
        self._applied_audio_live: bool | None = None
        self._reported_audio: tuple[AudioActual, str | None] | None = None
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for name, target in (
            ("output-supervisor", self._output_supervisor),
            ("input-supervisor", self._input_supervisor),
            ("media-monitor", self._monitor_loop),
        ):
            thread = threading.Thread(name=name, target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop_event.set()
        self._input_failed.set()
        self._output_failed.set()
        self._stop_input_pipeline()
        self._stop_output_pipeline()
        for thread in self._threads:
            thread.join(timeout=3.0)

    def _output_supervisor(self) -> None:
        while not self._stop_event.is_set():
            self._output_failed.clear()
            try:
                self._start_output_pipeline()
                while not self._stop_event.is_set() and not self._output_failed.wait(0.5):
                    pass
            except Exception as exc:  # pragma: no cover - exercised in Docker PoC
                error_name = type(exc).__name__
                LOGGER.error("Failed to start output pipeline (%s)", error_name)
                self.state.mark_output(False, f"OUTPUT_START_FAILED:{error_name}")
            finally:
                self._stop_output_pipeline()

            if not self._stop_event.wait(self.settings.restart_delay_seconds):
                LOGGER.info("Retrying output pipeline")

    def _input_supervisor(self) -> None:
        while not self._stop_event.is_set():
            self._input_failed.clear()
            try:
                self._start_input_pipeline()
                while not self._stop_event.is_set() and not self._input_failed.wait(0.5):
                    pass
            except Exception as exc:  # pragma: no cover - exercised in Docker PoC
                error_name = type(exc).__name__
                LOGGER.info("Input is not available yet (%s)", error_name)
                self.state.set_error(f"INPUT_START_FAILED:{error_name}")
            finally:
                self._stop_input_pipeline()

            if not self._stop_event.wait(self.settings.restart_delay_seconds):
                LOGGER.debug("Retrying input pipeline")

    def _start_output_pipeline(self) -> None:
        audio_encoder, rtmp_sink = _element_chain()
        output_url = _gst_quote(self.settings.output_url)
        key_int = max(1, self.settings.fps * 2)

        pipeline_description = f"""
            input-selector name=vsel sync-streams=true sync-mode=clock
                cache-buffers=true drop-backwards=true
                ! queue
                ! videoconvert
                ! x264enc tune=zerolatency speed-preset=veryfast
                    bitrate={self.settings.video_bitrate_kbps}
                    key-int-max={key_int} bframes=0
                ! video/x-h264,profile=main
                ! h264parse config-interval=-1
                ! queue
                ! mux.

            videotestsrc is-live=true pattern=black
                ! video/x-raw,width={self.settings.width},height={self.settings.height},framerate={self.settings.fps}/1
                ! textoverlay text="IRLight - waiting for input"
                    halignment=center valignment=center
                    font-desc="Sans 30" shaded-background=true
                ! queue leaky=downstream max-size-buffers=2
                ! vsel.sink_0

            intervideosrc channel=irlight-live-video is-live=true do-timestamp=true
                ! videoconvert
                ! videoscale
                ! videorate
                ! video/x-raw,width={self.settings.width},height={self.settings.height},framerate={self.settings.fps}/1
                ! queue leaky=downstream max-size-buffers=2
                ! vsel.sink_1

            input-selector name=asel sync-streams=true sync-mode=clock
                cache-buffers=true drop-backwards=true
                ! queue
                ! audioconvert
                ! audioresample
                ! audio/x-raw,format=F32LE,rate=48000,channels=2
                ! {audio_encoder}
                ! aacparse
                ! queue
                ! mux.

            audiotestsrc is-live=true wave=silence
                ! audio/x-raw,format=F32LE,rate=48000,channels=2
                ! queue
                ! asel.sink_0

            interaudiosrc channel=irlight-live-audio is-live=true do-timestamp=true
                ! audioconvert
                ! audioresample
                ! audio/x-raw,format=F32LE,rate=48000,channels=2
                ! queue
                ! asel.sink_1

            flvmux name=mux streamable=true
                ! queue
                ! {rtmp_sink} location="{output_url}"
        """

        pipeline = Gst.parse_launch(pipeline_description)
        if not isinstance(pipeline, Gst.Pipeline):
            raise RuntimeError("Output description did not produce a Gst.Pipeline")

        video_selector = pipeline.get_by_name("vsel")
        audio_selector = pipeline.get_by_name("asel")
        if video_selector is None or audio_selector is None:
            raise RuntimeError("Output selectors were not created")

        with self._pipeline_lock:
            self._output_pipeline = pipeline
            self._video_selector = video_selector
            self._audio_selector = audio_selector
            self._video_standby_pad = video_selector.get_static_pad("sink_0")
            self._video_live_pad = video_selector.get_static_pad("sink_1")
            self._audio_silence_pad = audio_selector.get_static_pad("sink_0")
            self._audio_live_pad = audio_selector.get_static_pad("sink_1")
            self._applied_video_live = None
            self._applied_audio_live = None
            self._set_video_selector(False)
            self._set_audio_selector(False)

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_output_message)

        result = pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Output pipeline refused PLAYING state")
        LOGGER.info("Output pipeline started")

    def _stop_output_pipeline(self) -> None:
        with self._pipeline_lock:
            pipeline = self._output_pipeline
            self._output_pipeline = None
            self._video_selector = None
            self._audio_selector = None
            self._video_standby_pad = None
            self._video_live_pad = None
            self._audio_silence_pad = None
            self._audio_live_pad = None
            self._applied_video_live = None
            self._applied_audio_live = None

        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        self.state.mark_output(False)

    def _start_input_pipeline(self) -> None:
        input_uri = _gst_quote(self.settings.input_uri)
        pipeline_description = f"""
            uridecodebin uri="{input_uri}" name=decode

            decode.
                ! queue
                ! videoconvert
                ! videoscale
                ! videorate
                ! video/x-raw,width={self.settings.width},height={self.settings.height},framerate={self.settings.fps}/1
                ! identity name=video_probe signal-handoffs=true
                ! intervideosink channel=irlight-live-video sync=false

            decode.
                ! queue
                ! audioconvert
                ! audioresample
                ! audio/x-raw,format=F32LE,rate=48000,channels=2
                ! identity name=audio_probe signal-handoffs=true
                ! interaudiosink channel=irlight-live-audio sync=false
        """

        pipeline = Gst.parse_launch(pipeline_description)
        if not isinstance(pipeline, Gst.Pipeline):
            raise RuntimeError("Input description did not produce a Gst.Pipeline")

        video_probe = pipeline.get_by_name("video_probe")
        audio_probe = pipeline.get_by_name("audio_probe")
        if video_probe is None or audio_probe is None:
            raise RuntimeError("Input probes were not created")
        video_probe.connect("handoff", self._on_video_handoff)
        audio_probe.connect("handoff", self._on_audio_handoff)

        with self._pipeline_lock:
            self._input_pipeline = pipeline

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_input_message)

        result = pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Input pipeline refused PLAYING state")
        LOGGER.debug("Input pipeline started")

    def _stop_input_pipeline(self) -> None:
        with self._pipeline_lock:
            pipeline = self._input_pipeline
            self._input_pipeline = None

        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)

    def _on_output_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            identity = _gerror_identity(error)
            LOGGER.error("Output pipeline error (%s)", identity)
            self.state.mark_output(False, f"OUTPUT_PIPELINE_ERROR:{identity}")
            self._output_failed.set()
        elif message.type == Gst.MessageType.EOS:
            self.state.mark_output(False, "OUTPUT_PIPELINE_EOS")
            self._output_failed.set()
        elif message.type == Gst.MessageType.STATE_CHANGED:
            with self._pipeline_lock:
                pipeline = self._output_pipeline
            if message.src == pipeline:
                _old, new, _pending = message.parse_state_changed()
                if new == Gst.State.PLAYING:
                    self.state.mark_output(True)

    def _on_input_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            identity = _gerror_identity(error)
            LOGGER.info("Input pipeline ended (%s)", identity)
            self.state.set_error(f"INPUT_PIPELINE_ERROR:{identity}")
            self._input_failed.set()
        elif message.type == Gst.MessageType.EOS:
            LOGGER.info("Input pipeline reached EOS")
            self._input_failed.set()

    def _on_video_handoff(self, _identity: Gst.Element, _buffer: Gst.Buffer) -> None:
        with self._frame_lock:
            self._last_video_mono = time.monotonic()
            self._last_video_iso = utc_now_iso()
            self._ever_received_input = True

    def _on_audio_handoff(self, _identity: Gst.Element, _buffer: Gst.Buffer) -> None:
        with self._frame_lock:
            self._last_audio_mono = time.monotonic()
            self._last_audio_iso = utc_now_iso()
            self._ever_received_input = True

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(0.25):
            now = time.monotonic()
            with self._frame_lock:
                last_video_mono = self._last_video_mono
                last_audio_mono = self._last_audio_mono
                last_video_iso = self._last_video_iso
                last_audio_iso = self._last_audio_iso
                ever_received_input = self._ever_received_input

            video_fresh = (
                last_video_mono is not None
                and now - last_video_mono <= self.settings.input_stale_seconds
            )
            audio_fresh = (
                last_audio_mono is not None
                and now - last_audio_mono <= self.settings.input_stale_seconds
            )

            if video_fresh:
                if self._stable_since is None:
                    self._stable_since = now
            else:
                self._stable_since = None

            video_live = bool(
                video_fresh
                and self._stable_since is not None
                and now - self._stable_since >= self.settings.recovery_stable_seconds
            )
            self._set_video_selector(video_live)

            snapshot = self.state.snapshot()
            desired = AudioMode(snapshot["audio"]["desired"])
            if desired == AudioMode.MUTED:
                audio_live = False
                actual = AudioActual.MUTED
                audio_reason = None
            elif video_live and audio_fresh:
                audio_live = True
                actual = AudioActual.LIVE
                audio_reason = None
            else:
                audio_live = False
                actual = AudioActual.MUTED
                audio_reason = "INPUT_AUDIO_UNAVAILABLE"

            self._set_audio_selector(audio_live)
            reported = (actual, audio_reason)
            if self._reported_audio != reported:
                self.state.mark_audio_actual(actual, audio_reason)
                self._reported_audio = reported

            if video_live:
                session_status = "LIVE"
                display_source = "LIVE"
            elif ever_received_input:
                session_status = "HOLDING"
                display_source = "STANDBY"
            else:
                session_status = "READY"
                display_source = "STANDBY"

            self.state.mark_input(
                connected=video_fresh or audio_fresh,
                has_video=video_fresh,
                has_audio=audio_fresh,
                session_status=session_status,
                display_source=display_source,
                last_video_at=last_video_iso,
                last_audio_at=last_audio_iso,
            )

    def _set_video_selector(self, use_live: bool) -> None:
        with self._pipeline_lock:
            if self._video_selector is None or self._applied_video_live == use_live:
                return
            target = self._video_live_pad if use_live else self._video_standby_pad
            if target is None:
                return
            self._video_selector.set_property("active-pad", target)
            self._applied_video_live = use_live
            LOGGER.info("Video source switched to %s", "LIVE" if use_live else "STANDBY")

    def _set_audio_selector(self, use_live: bool) -> None:
        with self._pipeline_lock:
            if self._audio_selector is None or self._applied_audio_live == use_live:
                return
            target = self._audio_live_pad if use_live else self._audio_silence_pad
            if target is None:
                return
            self._audio_selector.set_property("active-pad", target)
            self._applied_audio_live = use_live
            LOGGER.info("Audio source switched to %s", "LIVE" if use_live else "SILENCE")


class ControlHandler(BaseHTTPRequestHandler):
    state: RuntimeState
    settings: Settings
    server_version = "IRLightPoC/0.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._send_html(STATIC_INDEX.read_text(encoding="utf-8"))
            return
        if self.path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True, "state": self.state.snapshot()})
            return
        if self.path == "/api/state":
            payload = self.state.snapshot()
            payload["phase"] = "phase-0-vertical-slice"
            payload["connection"] = {
                "ingest_url": self.settings.public_ingest_url,
                "preview_url": self.settings.public_preview_url,
            }
            self._send_json(HTTPStatus.OK, payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_PUT(self) -> None:  # noqa: N802
        if self.path != "/api/audio":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        try:
            payload = self._read_json(max_bytes=4096)
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            mode = AudioMode(str(payload.get("mode", "")))
            expected_raw = payload.get("expected_version")
            expected_version = None if expected_raw is None else int(expected_raw)
            snapshot = self.state.set_audio_desired(
                mode,
                expected_version=expected_version,
            )
        except VersionConflictError as exc:
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "error": "version_conflict",
                    "expected": exc.expected,
                    "actual": exc.actual,
                    "state": self.state.snapshot(),
                },
            )
            return
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "detail": str(exc)})
            return

        self._send_json(HTTPStatus.ACCEPTED, snapshot)

    def _read_json(self, *, max_bytes: int) -> Any:
        raw_length = self.headers.get("Content-Length", "0")
        length = int(raw_length)
        if length <= 0 or length > max_bytes:
            raise ValueError(f"Content-Length must be between 1 and {max_bytes}")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'",
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: object) -> None:
        LOGGER.info("HTTP %s - %s", self.address_string(), format_string % args)


def start_http_server(settings: Settings, state: RuntimeState) -> ThreadingHTTPServer:
    handler = type(
        "BoundControlHandler",
        (ControlHandler,),
        {"settings": settings, "state": state},
    )
    server = ThreadingHTTPServer((settings.http_host, settings.http_port), handler)
    thread = threading.Thread(name="control-http", target=server.serve_forever, daemon=True)
    thread.start()
    LOGGER.info("Control UI listening on %s:%d", settings.http_host, settings.http_port)
    return server


def main() -> int:
    logging.basicConfig(
        level=os.getenv("IRLIGHT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    Gst.init(None)

    settings = Settings.from_env()
    state = RuntimeState()
    engine = ContinuityEngine(settings, state)
    server = start_http_server(settings, state)
    loop = GLib.MainLoop()
    stopping = threading.Event()

    def request_shutdown(_signum: int, _frame: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        LOGGER.info("Shutdown requested")
        GLib.idle_add(loop.quit)

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    engine.start()
    try:
        loop.run()
    finally:
        server.shutdown()
        server.server_close()
        engine.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
