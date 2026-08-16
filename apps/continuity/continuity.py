from __future__ import annotations

import json
import logging
import os
import signal
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from state import ActualAudio, AudioMode, ContinuityState, VideoSource  # noqa: E402


LOG = logging.getLogger("irlight.continuity")


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


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else default
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def redact_url(url: str) -> str:
    """Avoid writing a stream key/userinfo to status or logs."""
    if "/" not in url:
        return "<configured>"
    scheme, _, rest = url.partition("://")
    host, separator, _path = rest.partition("/")
    if not separator:
        return f"{scheme}://{host}/…"
    return f"{scheme}://{host}/…"


def _gst_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class ContinuityPipeline:
    """Single-session Phase 0 continuity engine.

    The output pipeline is created once and never torn down: standby video and
    silence audio are always available through input-selectors, so the RTMP
    egress session survives input loss. The input pipeline is a separate,
    disposable unit that is rebuilt from scratch on each reconnect attempt.
    Rebuilding a fresh pipeline avoids the GStreamer quirks of adding or
    recycling an uridecodebin inside a running pipeline.
    """

    def __init__(self) -> None:
        self.input_uri = os.getenv("INPUT_URI", "rtsp://mediamtx:8554/live/input")
        self.egress_url = os.getenv(
            "EGRESS_URL", "rtmp://mediamtx:1935/output/relay"
        )
        self.state_dir = Path(os.getenv("STATE_DIR", "/state"))
        self.control_path = self.state_dir / "control.json"
        self.status_path = self.state_dir / "status.json"

        self.width = env_int("VIDEO_WIDTH", 1280)
        self.height = env_int("VIDEO_HEIGHT", 720)
        self.fps = env_int("VIDEO_FPS", 30)
        self.bitrate_kbps = env_int("VIDEO_BITRATE_KBPS", 3500)
        self.source_retry_seconds = env_float("SOURCE_RETRY_SECONDS", 3.0)
        self.model = ContinuityState(
            input_timeout=env_float("INPUT_TIMEOUT_SECONDS", 1.5),
            stable_window=env_float("RECOVERY_STABLE_SECONDS", 3.0),
        )

        self.pipeline: Gst.Pipeline | None = None
        self.video_selector: Gst.Element | None = None
        self.audio_selector: Gst.Element | None = None
        self.fallback_video_pad: Gst.Pad | None = None
        self.fallback_audio_pad: Gst.Pad | None = None
        self.live_video_pad: Gst.Pad | None = None
        self.live_audio_pad: Gst.Pad | None = None

        self._stop_event = threading.Event()
        self._input_failed = threading.Event()
        self._input_pipeline: Gst.Pipeline | None = None
        self._input_thread: threading.Thread | None = None

        self.source_generation = 0
        self.source_failed = False
        self.last_control_version = 0
        self.last_command_id: str | None = None
        self.last_error: str | None = None
        self.started_at = time.time()
        self.main_loop = GLib.MainLoop()

    def run(self) -> None:
        Gst.init(None)
        self._ensure_default_control()
        self._build_output_pipeline()
        assert self.pipeline is not None

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_output_message)

        GLib.timeout_add(250, self._reconcile)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._stop)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._stop)

        result = self.pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("failed to start output pipeline")

        self._input_thread = threading.Thread(
            name="input-supervisor", target=self._input_supervisor, daemon=True
        )
        self._input_thread.start()

        LOG.info(
            "continuity pipeline started input=%s egress=%s profile=COMPOSITED_VIDEO_POC",
            self.input_uri,
            redact_url(self.egress_url),
        )
        self.main_loop.run()
        self._stop_event.set()
        self._input_failed.set()
        self._stop_input_pipeline()
        if self._input_thread is not None:
            self._input_thread.join(timeout=3.0)
        self.pipeline.set_state(Gst.State.NULL)

    def _ensure_default_control(self) -> None:
        if not self.control_path.exists():
            atomic_write_json(
                self.control_path,
                {
                    "audio_mode": AudioMode.LIVE,
                    "version": 0,
                    "command_id": None,
                    "updated_at": time.time(),
                },
            )

    def _build_output_pipeline(self) -> None:
        egress_literal = json.dumps(self.egress_url)
        key_int = max(1, self.fps * 2)
        description = f"""
            input-selector name=video_selector sync-streams=true cache-buffers=true !
                queue max-size-time=2000000000 !
                videoconvert !
                video/x-raw,format=I420,width={self.width},height={self.height},framerate={self.fps}/1 !
                x264enc tune=zerolatency speed-preset=veryfast bitrate={self.bitrate_kbps} key-int-max={key_int} !
                h264parse config-interval=-1 ! queue ! mux.
            input-selector name=audio_selector sync-streams=true cache-buffers=true !
                queue max-size-time=2000000000 !
                audioconvert ! audioresample !
                audio/x-raw,rate=48000,channels=2 !
                avenc_aac bitrate=128000 ! aacparse ! queue ! mux.
            flvmux name=mux streamable=true !
                rtmpsink location={egress_literal} sync=false async=false

            videotestsrc name=standby_video is-live=true pattern=black !
                video/x-raw,width={self.width},height={self.height},framerate={self.fps}/1 !
                queue ! video_selector.sink_0

            intervideosrc name=live_video channel=irlight-live-video do-timestamp=true !
                videoconvert ! videoscale ! videorate !
                video/x-raw,width={self.width},height={self.height},framerate={self.fps}/1 !
                queue leaky=downstream max-size-buffers=2 ! video_selector.sink_1

            audiotestsrc name=silence_source is-live=true wave=silence !
                audio/x-raw,rate=48000,channels=2 !
                queue ! audio_selector.sink_0

            interaudiosrc name=live_audio channel=irlight-live-audio do-timestamp=true !
                audioconvert ! audioresample !
                audio/x-raw,rate=48000,channels=2 !
                queue leaky=downstream max-size-buffers=2 ! audio_selector.sink_1
        """
        parsed = Gst.parse_launch(description)
        if not isinstance(parsed, Gst.Pipeline):
            pipeline = Gst.Pipeline.new("irlight-continuity")
            pipeline.add(parsed)
            self.pipeline = pipeline
        else:
            self.pipeline = parsed

        assert self.pipeline is not None
        self.video_selector = self.pipeline.get_by_name("video_selector")
        self.audio_selector = self.pipeline.get_by_name("audio_selector")
        if self.video_selector is None or self.audio_selector is None:
            raise RuntimeError("input selectors were not created")
        self.fallback_video_pad = self.video_selector.get_static_pad("sink_0")
        self.fallback_audio_pad = self.audio_selector.get_static_pad("sink_0")
        self.live_video_pad = self.video_selector.get_static_pad("sink_1")
        self.live_audio_pad = self.audio_selector.get_static_pad("sink_1")
        if any(
            pad is None
            for pad in (
                self.fallback_video_pad,
                self.fallback_audio_pad,
                self.live_video_pad,
                self.live_audio_pad,
            )
        ):
            raise RuntimeError("selector sink pads were not created")
        self._activate_pad(self.video_selector, self.fallback_video_pad)
        self._activate_pad(self.audio_selector, self.fallback_audio_pad)

    def _input_supervisor(self) -> None:
        while not self._stop_event.is_set():
            self._input_failed.clear()
            try:
                self._start_input_pipeline()
                while (
                    not self._stop_event.is_set()
                    and not self._input_failed.wait(0.5)
                ):
                    pass
            except Exception as exc:  # pragma: no cover - exercised in Docker PoC
                LOG.info("Input is not available yet (%s)", type(exc).__name__)
                self.last_error = f"INPUT_START_FAILED:{type(exc).__name__}"
            finally:
                self._stop_input_pipeline()

            if not self._stop_event.wait(self.source_retry_seconds):
                LOG.debug("Retrying input pipeline")

    def _start_input_pipeline(self) -> None:
        input_uri = _gst_quote(self.input_uri)
        description = f"""
            uridecodebin uri="{input_uri}" name=decode

            decode.
                ! queue
                ! videoconvert
                ! videoscale
                ! videorate
                ! video/x-raw,width={self.width},height={self.height},framerate={self.fps}/1
                ! identity name=video_probe signal-handoffs=true
                ! intervideosink channel=irlight-live-video sync=false

            decode.
                ! queue
                ! audioconvert
                ! audioresample
                ! audio/x-raw,rate=48000,channels=2
                ! identity name=audio_probe signal-handoffs=true
                ! interaudiosink channel=irlight-live-audio sync=false
        """
        pipeline = Gst.parse_launch(description)
        if not isinstance(pipeline, Gst.Pipeline):
            raise RuntimeError("Input description did not produce a Gst.Pipeline")

        video_probe = pipeline.get_by_name("video_probe")
        audio_probe = pipeline.get_by_name("audio_probe")
        if video_probe is None or audio_probe is None:
            raise RuntimeError("Input probes were not created")
        video_probe.connect("handoff", self._on_video_handoff)
        audio_probe.connect("handoff", self._on_audio_handoff)

        self._input_pipeline = pipeline
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_input_message)

        self.source_generation += 1
        self.source_failed = False
        self.last_error = None
        result = pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Input pipeline refused PLAYING state")
        LOG.info("input pipeline started generation=%s", self.source_generation)

    def _stop_input_pipeline(self) -> None:
        pipeline = self._input_pipeline
        self._input_pipeline = None
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)

    def _on_input_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            identity = f"{error.domain}:{error.code}"
            LOG.info("Input pipeline ended (%s)", identity)
            self.last_error = f"INPUT_PIPELINE_ERROR:{identity}"
            self._input_failed.set()
        elif message.type == Gst.MessageType.EOS:
            LOG.info("Input pipeline reached EOS")
            self._input_failed.set()

    def _on_output_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            identity = f"{error.domain}:{error.code}"
            LOG.error("Output pipeline error (%s)", identity)
            self.last_error = f"OUTPUT_PIPELINE_ERROR:{identity}"
            self._stop()
        elif message.type == Gst.MessageType.EOS:
            self.last_error = "OUTPUT_PIPELINE_EOS"
            self._stop()

    def _on_video_handoff(self, _identity: Gst.Element, _buffer: Gst.Buffer) -> None:
        self.model.observe_video(time.monotonic())

    def _on_audio_handoff(self, _identity: Gst.Element, _buffer: Gst.Buffer) -> None:
        self.model.observe_audio(time.monotonic())

    def _reconcile(self) -> bool:
        now_wall = time.time()
        now_mono = time.monotonic()
        control = read_json(
            self.control_path,
            {
                "audio_mode": AudioMode.LIVE,
                "version": 0,
                "command_id": None,
            },
        )
        try:
            desired = AudioMode(str(control.get("audio_mode", AudioMode.LIVE)))
        except ValueError:
            desired = AudioMode.LIVE
            self.last_error = "INVALID_AUDIO_MODE"
        self.model.set_audio_mode(desired)
        self.last_control_version = int(control.get("version", 0))
        self.last_command_id = control.get("command_id")

        decision = self.model.decide(now_mono)
        assert self.video_selector is not None and self.audio_selector is not None
        assert (
            self.fallback_video_pad is not None
            and self.fallback_audio_pad is not None
            and self.live_video_pad is not None
            and self.live_audio_pad is not None
        )

        if decision.video_source is VideoSource.LIVE:
            self._activate_pad(self.video_selector, self.live_video_pad)
        else:
            self._activate_pad(self.video_selector, self.fallback_video_pad)

        if decision.actual_audio is ActualAudio.LIVE:
            self._activate_pad(self.audio_selector, self.live_audio_pad)
        else:
            self._activate_pad(self.audio_selector, self.fallback_audio_pad)

        atomic_write_json(
            self.status_path,
            {
                "profile": "COMPOSITED_VIDEO_POC",
                "session_status": decision.session_status,
                "video_source": decision.video_source,
                "desired_audio_mode": desired,
                "actual_audio_mode": decision.actual_audio,
                "input_video_recent": decision.video_recent,
                "input_audio_recent": decision.audio_recent,
                "control_version": self.last_control_version,
                "command_id": self.last_command_id,
                "source_generation": self.source_generation,
                "source_retrying": self._input_pipeline is None,
                "last_error": self.last_error,
                "egress": redact_url(self.egress_url),
                "started_at": self.started_at,
                "updated_at": now_wall,
            },
        )
        return True

    @staticmethod
    def _activate_pad(selector: Gst.Element, pad: Gst.Pad) -> None:
        if selector.get_property("active-pad") != pad:
            selector.set_property("active-pad", pad)

    def _stop(self) -> bool:
        if self.main_loop.is_running():
            self.main_loop.quit()
        return False


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ContinuityPipeline().run()


if __name__ == "__main__":
    main()
