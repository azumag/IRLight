from __future__ import annotations

import json
import logging
import os
import signal
import tempfile
import time
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from state import ActualAudio, AudioMode, ContinuityState, VideoSource


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


class ContinuityPipeline:
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
        self.live_elements: list[Gst.Element] = []
        self.live_source: Gst.Element | None = None
        self.source_generation = 0
        self.source_failed = False
        self.next_source_retry_at = 0.0
        self.last_control_version = 0
        self.last_command_id: str | None = None
        self.last_error: str | None = None
        self.started_at = time.time()
        self.main_loop = GLib.MainLoop()

    def run(self) -> None:
        Gst.init(None)
        self._ensure_default_control()
        self._build_pipeline()
        assert self.pipeline is not None

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        self._build_live_source()
        GLib.timeout_add(250, self._reconcile)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._stop)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._stop)

        result = self.pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("failed to start GStreamer pipeline")

        LOG.info(
            "continuity pipeline started input=%s egress=%s profile=COMPOSITED_VIDEO_POC",
            self.input_uri,
            redact_url(self.egress_url),
        )
        self.main_loop.run()
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

    def _build_pipeline(self) -> None:
        egress_literal = json.dumps(self.egress_url)
        description = f"""
            input-selector name=video_selector sync-streams=true cache-buffers=true !
                queue max-size-time=2000000000 !
                videoconvert !
                video/x-raw,format=I420,width={self.width},height={self.height},framerate={self.fps}/1 !
                x264enc tune=zerolatency speed-preset=veryfast bitrate={self.bitrate_kbps} key-int-max={self.fps * 2} !
                h264parse config-interval=-1 ! queue ! mux.
            input-selector name=audio_selector sync-streams=true cache-buffers=true !
                queue max-size-time=2000000000 !
                audioconvert ! audioresample !
                audio/x-raw,rate=48000,channels=2 !
                avenc_aac bitrate=128000 ! aacparse ! queue ! mux.
            flvmux name=mux streamable=true !
                rtmpsink location={egress_literal} sync=false async=false
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

        self.fallback_video_pad = self._add_fallback_video()
        self.fallback_audio_pad = self._add_fallback_audio()
        self._activate_pad(self.video_selector, self.fallback_video_pad)
        self._activate_pad(self.audio_selector, self.fallback_audio_pad)

    @staticmethod
    def _link_many(elements: list[Gst.Element], label: str) -> None:
        for left, right in zip(elements, elements[1:]):
            if not left.link(right):
                raise RuntimeError(
                    f"failed to link {label}: {left.name} -> {right.name}"
                )

    def _request_selector_pad(self, selector: Gst.Element) -> Gst.Pad:
        pad = selector.request_pad_simple("sink_%u")
        if pad is None:
            pad = selector.get_request_pad("sink_%u")
        if pad is None:
            raise RuntimeError(f"could not request selector pad from {selector.name}")
        return pad

    def _add_fallback_video(self) -> Gst.Pad:
        assert self.pipeline is not None and self.video_selector is not None
        source = Gst.ElementFactory.make("videotestsrc", "standby_video")
        source.set_property("is-live", True)
        source.set_property("pattern", 2)
        caps = Gst.ElementFactory.make("capsfilter", "standby_video_caps")
        caps.set_property(
            "caps",
            Gst.Caps.from_string(
                f"video/x-raw,width={self.width},height={self.height},framerate={self.fps}/1"
            ),
        )
        overlay = Gst.ElementFactory.make("textoverlay", "standby_text")
        overlay.set_property("text", "IRLight - input disconnected")
        overlay.set_property("font-desc", "Sans 30")
        convert = Gst.ElementFactory.make("videoconvert", "standby_convert")
        queue = Gst.ElementFactory.make("queue", "standby_video_queue")
        elements = [source, caps, overlay, convert, queue]
        for element in elements:
            if element is None:
                raise RuntimeError("required standby video plugin is unavailable")
            self.pipeline.add(element)
        self._link_many(elements, "standby video")
        selector_pad = self._request_selector_pad(self.video_selector)
        if queue.get_static_pad("src").link(selector_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError("failed to attach standby video to selector")
        return selector_pad

    def _add_fallback_audio(self) -> Gst.Pad:
        assert self.pipeline is not None and self.audio_selector is not None
        source = Gst.ElementFactory.make("audiotestsrc", "silence_source")
        source.set_property("is-live", True)
        source.set_property("wave", 4)
        caps = Gst.ElementFactory.make("capsfilter", "silence_caps")
        caps.set_property(
            "caps", Gst.Caps.from_string("audio/x-raw,rate=48000,channels=2")
        )
        convert = Gst.ElementFactory.make("audioconvert", "silence_convert")
        resample = Gst.ElementFactory.make("audioresample", "silence_resample")
        queue = Gst.ElementFactory.make("queue", "silence_queue")
        elements = [source, caps, convert, resample, queue]
        for element in elements:
            if element is None:
                raise RuntimeError("required silence audio plugin is unavailable")
            self.pipeline.add(element)
        self._link_many(elements, "silence audio")
        selector_pad = self._request_selector_pad(self.audio_selector)
        if queue.get_static_pad("src").link(selector_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError("failed to attach silence audio to selector")
        return selector_pad

    def _build_live_source(self) -> None:
        assert self.pipeline is not None
        self._remove_live_source()
        self.source_generation += 1
        generation = self.source_generation
        source = Gst.ElementFactory.make("uridecodebin", f"live_source_{generation}")
        if source is None:
            raise RuntimeError("uridecodebin plugin is unavailable")
        source.set_property("uri", self.input_uri)
        source.connect("pad-added", self._on_live_pad_added, generation)
        source.connect("source-setup", self._on_source_setup)
        self.pipeline.add(source)
        self.live_source = source
        self.live_elements = [source]
        self.source_failed = False
        source.sync_state_with_parent()
        LOG.info("created live source generation=%s", generation)

    def _on_source_setup(self, _decodebin: Gst.Element, source: Gst.Element) -> None:
        if source.find_property("latency") is not None:
            source.set_property("latency", 500)
        if source.find_property("tcp-timeout") is not None:
            source.set_property("tcp-timeout", 3_000_000)

    def _on_live_pad_added(
        self, _source: Gst.Element, pad: Gst.Pad, generation: int
    ) -> None:
        if generation != self.source_generation or self.pipeline is None:
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        structure = caps.get_structure(0) if caps and caps.get_size() else None
        media_type = structure.get_name() if structure else ""
        if media_type.startswith("video/") and self.live_video_pad is None:
            self.live_video_pad = self._attach_live_video(pad, generation)
        elif media_type.startswith("audio/") and self.live_audio_pad is None:
            self.live_audio_pad = self._attach_live_audio(pad, generation)

    def _attach_live_video(self, source_pad: Gst.Pad, generation: int) -> Gst.Pad:
        assert self.pipeline is not None and self.video_selector is not None
        queue = Gst.ElementFactory.make("queue", f"live_video_queue_{generation}")
        convert = Gst.ElementFactory.make("videoconvert", f"live_video_convert_{generation}")
        scale = Gst.ElementFactory.make("videoscale", f"live_video_scale_{generation}")
        rate = Gst.ElementFactory.make("videorate", f"live_video_rate_{generation}")
        caps = Gst.ElementFactory.make("capsfilter", f"live_video_caps_{generation}")
        caps.set_property(
            "caps",
            Gst.Caps.from_string(
                f"video/x-raw,width={self.width},height={self.height},framerate={self.fps}/1"
            ),
        )
        elements = [queue, convert, scale, rate, caps]
        for element in elements:
            if element is None:
                raise RuntimeError("required live video plugin is unavailable")
            self.pipeline.add(element)
            self.live_elements.append(element)
        self._link_many(elements, "live video conversion")
        if source_pad.link(queue.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            raise RuntimeError("failed to link live video decode pad")
        selector_pad = self._request_selector_pad(self.video_selector)
        if caps.get_static_pad("src").link(selector_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError("failed to attach live video to selector")
        caps.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, self._on_video_buffer
        )
        for element in elements:
            element.sync_state_with_parent()
        LOG.info("live video pad attached generation=%s", generation)
        return selector_pad

    def _attach_live_audio(self, source_pad: Gst.Pad, generation: int) -> Gst.Pad:
        assert self.pipeline is not None and self.audio_selector is not None
        queue = Gst.ElementFactory.make("queue", f"live_audio_queue_{generation}")
        convert = Gst.ElementFactory.make("audioconvert", f"live_audio_convert_{generation}")
        resample = Gst.ElementFactory.make("audioresample", f"live_audio_resample_{generation}")
        caps = Gst.ElementFactory.make("capsfilter", f"live_audio_caps_{generation}")
        caps.set_property(
            "caps", Gst.Caps.from_string("audio/x-raw,rate=48000,channels=2")
        )
        elements = [queue, convert, resample, caps]
        for element in elements:
            if element is None:
                raise RuntimeError("required live audio plugin is unavailable")
            self.pipeline.add(element)
            self.live_elements.append(element)
        self._link_many(elements, "live audio conversion")
        if source_pad.link(queue.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            raise RuntimeError("failed to link live audio decode pad")
        selector_pad = self._request_selector_pad(self.audio_selector)
        if caps.get_static_pad("src").link(selector_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError("failed to attach live audio to selector")
        caps.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, self._on_audio_buffer
        )
        for element in elements:
            element.sync_state_with_parent()
        LOG.info("live audio pad attached generation=%s", generation)
        return selector_pad

    def _on_video_buffer(self, _pad: Gst.Pad, _info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        self.model.observe_video(time.monotonic())
        return Gst.PadProbeReturn.OK

    def _on_audio_buffer(self, _pad: Gst.Pad, _info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        self.model.observe_audio(time.monotonic())
        return Gst.PadProbeReturn.OK

    def _remove_live_source(self) -> None:
        if self.pipeline is None:
            return
        if self.video_selector is not None and self.live_video_pad is not None:
            self.video_selector.release_request_pad(self.live_video_pad)
        if self.audio_selector is not None and self.live_audio_pad is not None:
            self.audio_selector.release_request_pad(self.live_audio_pad)
        self.live_video_pad = None
        self.live_audio_pad = None

        for element in reversed(self.live_elements):
            try:
                element.set_state(Gst.State.NULL)
                self.pipeline.remove(element)
            except (TypeError, RuntimeError):
                LOG.exception("failed to remove live element %s", element.name)
        self.live_elements = []
        self.live_source = None

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
        assert self.fallback_video_pad is not None and self.fallback_audio_pad is not None

        if decision.video_source is VideoSource.LIVE and self.live_video_pad is not None:
            self._activate_pad(self.video_selector, self.live_video_pad)
        else:
            self._activate_pad(self.video_selector, self.fallback_video_pad)

        if decision.actual_audio is ActualAudio.LIVE and self.live_audio_pad is not None:
            self._activate_pad(self.audio_selector, self.live_audio_pad)
        else:
            self._activate_pad(self.audio_selector, self.fallback_audio_pad)

        if self.source_failed and now_mono >= self.next_source_retry_at:
            try:
                self._build_live_source()
            except Exception as exc:
                self.last_error = f"SOURCE_REBUILD_FAILED:{type(exc).__name__}"
                self.source_failed = True
                self.next_source_retry_at = now_mono + self.source_retry_seconds
                LOG.exception("live source rebuild failed")

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
                "source_retrying": self.source_failed,
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

    def _on_bus_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            if self._belongs_to_live_source(message.src):
                LOG.warning("live input error: %s debug=%s", error, debug)
                self.source_failed = True
                self.next_source_retry_at = time.monotonic() + self.source_retry_seconds
                self.last_error = f"INPUT_ERROR:{error.domain}:{error.code}"
                return
            self.last_error = f"PIPELINE_ERROR:{error.domain}:{error.code}"
            LOG.error("fatal pipeline error: %s debug=%s", error, debug)
            self._stop()
        elif message.type == Gst.MessageType.EOS:
            if self._belongs_to_live_source(message.src):
                self.source_failed = True
                self.next_source_retry_at = time.monotonic() + self.source_retry_seconds
                self.last_error = "INPUT_EOS"
            else:
                self.last_error = "PIPELINE_EOS"
                self._stop()

    @staticmethod
    def _belongs_to_live_source(element: Gst.Object | None) -> bool:
        current = element
        while current is not None:
            try:
                if current.get_name().startswith("live_"):
                    return True
                current = current.get_parent()
            except (AttributeError, RuntimeError):
                return False
        return False

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
