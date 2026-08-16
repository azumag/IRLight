import unittest

from state import AudioActual, AudioMode, RuntimeState, VersionConflictError


class RuntimeStateTest(unittest.TestCase):
    def test_put_is_idempotent_for_same_mode(self) -> None:
        state = RuntimeState()
        first = state.set_audio_desired(AudioMode.LIVE, expected_version=0)
        second = state.set_audio_desired(AudioMode.LIVE, expected_version=0)

        self.assertEqual(first["audio"]["version"], 0)
        self.assertEqual(second["audio"]["version"], 0)

    def test_state_change_increments_version_and_enters_applying(self) -> None:
        state = RuntimeState()
        snapshot = state.set_audio_desired(AudioMode.MUTED, expected_version=0)

        self.assertEqual(snapshot["audio"]["desired"], "MUTED")
        self.assertEqual(snapshot["audio"]["actual"], "APPLYING")
        self.assertEqual(snapshot["audio"]["version"], 1)

    def test_rejects_stale_expected_version(self) -> None:
        state = RuntimeState()
        state.set_audio_desired(AudioMode.MUTED, expected_version=0)

        with self.assertRaises(VersionConflictError) as context:
            state.set_audio_desired(AudioMode.LIVE, expected_version=0)

        self.assertEqual(context.exception.expected, 0)
        self.assertEqual(context.exception.actual, 1)

    def test_input_transition_does_not_reset_desired_mute(self) -> None:
        state = RuntimeState()
        state.set_audio_desired(AudioMode.MUTED, expected_version=0)
        state.mark_input(
            connected=False,
            has_video=False,
            has_audio=False,
            session_status="HOLDING",
            display_source="STANDBY",
        )
        state.mark_audio_actual(AudioActual.MUTED)

        snapshot = state.snapshot()
        self.assertEqual(snapshot["audio"]["desired"], "MUTED")
        self.assertEqual(snapshot["audio"]["actual"], "MUTED")
        self.assertEqual(snapshot["session_status"], "HOLDING")


if __name__ == "__main__":
    unittest.main()
