from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "continuity"))

from state import (  # noqa: E402
    ActualAudio,
    AudioMode,
    ContinuityState,
    SessionStatus,
    VideoSource,
)


class ContinuityStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = ContinuityState(input_timeout=1.5, stable_window=3.0)

    def test_starts_in_holding_with_silence(self) -> None:
        decision = self.state.decide(0.0)
        self.assertEqual(decision.session_status, SessionStatus.HOLDING)
        self.assertEqual(decision.video_source, VideoSource.STANDBY)
        self.assertEqual(decision.actual_audio, ActualAudio.SILENT_FALLBACK)

    def test_requires_stable_video_before_live(self) -> None:
        self.state.observe_video(10.0)
        self.state.observe_audio(10.0)
        decision = self.state.decide(10.5)
        self.assertEqual(decision.session_status, SessionStatus.STABILIZING)

        self.state.observe_video(11.0)
        self.state.observe_audio(11.0)
        self.state.observe_video(12.0)
        self.state.observe_audio(12.0)
        self.state.observe_video(13.1)
        self.state.observe_audio(13.1)
        decision = self.state.decide(13.1)
        self.assertEqual(decision.session_status, SessionStatus.LIVE)
        self.assertEqual(decision.actual_audio, ActualAudio.LIVE)

    def test_timeout_returns_to_standby(self) -> None:
        for timestamp in (1.0, 2.0, 3.0, 4.1):
            self.state.observe_video(timestamp)
            self.state.observe_audio(timestamp)
        self.assertEqual(self.state.decide(4.1).video_source, VideoSource.LIVE)
        decision = self.state.decide(6.0)
        self.assertEqual(decision.session_status, SessionStatus.HOLDING)
        self.assertEqual(decision.video_source, VideoSource.STANDBY)

    def test_mute_survives_disconnect_and_recovery(self) -> None:
        self.state.set_audio_mode(AudioMode.MUTED)
        self.assertEqual(self.state.decide(0.0).actual_audio, ActualAudio.MUTED)
        for timestamp in (10.0, 11.0, 12.0, 13.1):
            self.state.observe_video(timestamp)
            self.state.observe_audio(timestamp)
        decision = self.state.decide(13.1)
        self.assertEqual(decision.video_source, VideoSource.LIVE)
        self.assertEqual(decision.actual_audio, ActualAudio.MUTED)
        self.assertEqual(self.state.decide(20.0).actual_audio, ActualAudio.MUTED)


if __name__ == "__main__":
    unittest.main()
