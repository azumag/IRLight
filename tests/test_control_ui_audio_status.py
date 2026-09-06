from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "apps" / "control-api" / "static" / "index.html").read_text(
    encoding="utf-8"
)


class ControlUiAudioStatusContractTest(unittest.TestCase):
    def test_actual_audio_badge_does_not_treat_desired_mute_as_success(self) -> None:
        self.assertNotIn("desiredMuted ? 'ミュート中'", INDEX)
        self.assertIn("音声オン（ミュート未反映）", INDEX)
        self.assertIn("ミュート中（解除未反映）", INDEX)
        self.assertIn("SILENT_FALLBACK: '入力音声なし'", INDEX)

    def test_latest_control_command_must_be_acknowledged_before_next_action(self) -> None:
        self.assertIn("const commandIdentityValid = controlVersion === 0", INDEX)
        self.assertIn(
            "controlVersion !== null && controlVersion > 0 && typeof controlCommand === 'string' && controlCommand.length > 0 && controlCommand === runtimeCommand",
            INDEX,
        )
        self.assertIn(
            "const commandAcked = controlVersion === runtimeVersion && commandIdentityValid",
            INDEX,
        )
        self.assertIn(
            "button.disabled = applying || controlUnavailable || !statusAvailable || state.runtimeStale || !state.actualKnown || !state.commandAcked",
            INDEX,
        )
        self.assertIn("最新の指示を反映中…", INDEX)
        self.assertIn("ミュート適用待ち（ACK未確認）", INDEX)
        self.assertIn("指定 ${controlVersion} / 反映 ${runtimeVersion}", INDEX)

    def test_stale_or_unavailable_runtime_fails_closed_in_ui(self) -> None:
        self.assertIn("const RUNTIME_STALE_SECONDS = 3", INDEX)
        self.assertIn("runtimeAge > RUNTIME_STALE_SECONDS", INDEX)
        self.assertIn("statusAvailable = false", INDEX)
        self.assertIn("状態確認不能", INDEX)
        self.assertIn("状態を再取得しています", INDEX)

    def test_stale_runtime_hides_runtime_derived_values(self) -> None:
        self.assertIn("const runtimeTrusted = !state.runtimeStale", INDEX)
        self.assertIn("runtimeTrusted ? textForSession(r.session_status) : '状態確認不能'", INDEX)
        self.assertIn("runtimeTrusted ? (r.video_source === 'LIVE' ? '通常映像' : '待機画面') : '状態確認不能'", INDEX)
        self.assertIn("runtimeTrusted && Number.isInteger(r.control_version) ? r.control_version : '状態確認不能'", INDEX)

    def test_unavailable_status_hides_cached_runtime_and_control_values(self) -> None:
        self.assertIn("function renderUnavailable()", INDEX)
        self.assertIn("if (!statusAvailable) {\n    renderUnavailable();\n    return;", INDEX)
        self.assertIn("['video', 'inputVideo', 'inputAudio', 'desired', 'actual', 'version', 'updated']", INDEX)
        self.assertIn("$('session').lastElementChild.textContent = unknown", INDEX)
        self.assertIn("button.className = 'unknown'", INDEX)
        self.assertIn("button.unknown", INDEX)

    def test_unknown_runtime_audio_mode_fails_closed_in_ui(self) -> None:
        self.assertIn(
            "const actualKnown = actual === 'LIVE' || actual === 'MUTED' || actual === 'SILENT_FALLBACK'",
            INDEX,
        )
        self.assertIn("if (runtimeStale || !actualKnown)", INDEX)
        self.assertIn("state.runtimeStale || !state.actualKnown", INDEX)
        self.assertIn("実状態を確認できません", INDEX)

    def test_status_polling_is_single_flight(self) -> None:
        self.assertIn("if (refreshPromise) return refreshPromise", INDEX)
        self.assertIn("async function refreshAfterCurrent()", INDEX)
        self.assertIn("if (refreshPromise) await refreshPromise", INDEX)

    def test_unknown_command_outcome_resyncs_before_controls_reopen(self) -> None:
        self.assertIn("const idempotencyKey = crypto.randomUUID()", INDEX)
        self.assertIn("statusAvailable = false;\n    if (snapshot) render(snapshot);\n    await refreshAfterCurrent()", INDEX)
        self.assertIn("操作結果を確認できないため操作を停止しています", INDEX)
        self.assertIn("controlUnavailable = true", INDEX)
        self.assertIn("音声制御APIは利用できません", INDEX)


if __name__ == "__main__":
    unittest.main()
