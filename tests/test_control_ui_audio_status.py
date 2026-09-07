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

    def test_last_successful_status_check_remains_visible_when_current_state_is_unavailable(self) -> None:
        self.assertIn('<span class="label">最終確認</span><span id="checked" class="value">—</span>', INDEX)
        self.assertIn("function lastCheckedText()", INDEX)
        self.assertIn("snapshotReceivedAtMs === null", INDEX)
        self.assertIn("new Date(snapshotReceivedAtMs).toLocaleTimeString()", INDEX)
        self.assertIn("$('checked').textContent = lastCheckedText()", INDEX)
        self.assertIn("['video', 'inputVideo', 'inputAudio', 'desired', 'actual', 'version', 'updated']", INDEX)
        self.assertNotIn("'checked', 'updated'", INDEX)

    def test_control_api_reconnect_state_is_visible_without_reusing_cached_runtime(self) -> None:
        self.assertIn('<span class="label">管理接続</span><span id="connection" class="value" aria-describedby="connectionHelp">接続中…</span>', INDEX)
        self.assertIn("$('connection').textContent = '再接続中…'", INDEX)
        self.assertIn("$('connection').textContent = '接続済み'", INDEX)
        self.assertIn("$('checked').textContent = lastCheckedText()", INDEX)
        self.assertIn("statusAvailable = false", INDEX)
        self.assertIn("if (!statusAvailable) {\n    renderUnavailable();\n    return;", INDEX)

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

    def test_status_and_control_requests_have_a_bounded_timeout(self) -> None:
        self.assertIn("const REQUEST_TIMEOUT_MS = 5000", INDEX)
        self.assertIn("async function fetchWithTimeout(resource, options, consumeResponse)", INDEX)
        self.assertIn("const controller = new AbortController()", INDEX)
        self.assertIn("setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)", INDEX)
        self.assertIn("signal: controller.signal", INDEX)
        self.assertIn("return await consumeResponse(response)", INDEX)
        self.assertIn("clearTimeout(timeoutId)", INDEX)
        self.assertIn("fetchWithTimeout('/api/status', {cache:'no-store'}, async (response) =>", INDEX)
        self.assertIn("return await response.json()", INDEX)
        self.assertIn("fetchWithTimeout('/api/audio', {", INDEX)
        self.assertIn("async (response) => ({status: response.status, ok: response.ok})", INDEX)

    def test_cached_snapshot_ages_out_while_status_request_is_in_flight(self) -> None:
        self.assertIn("let snapshotReceivedAtMs = null", INDEX)
        self.assertIn("let snapshotRequestAgeSeconds = Number.POSITIVE_INFINITY", INDEX)
        self.assertIn("const requestStartedAtMs = Date.now()", INDEX)
        self.assertIn("const receivedAtMs = Date.now()", INDEX)
        self.assertIn("snapshotReceivedAtMs = receivedAtMs", INDEX)
        self.assertIn("snapshotRequestAgeSeconds = requestAgeSeconds >= 0", INDEX)
        self.assertIn("snapshotRequestAgeSeconds + (Date.now() - snapshotReceivedAtMs) / 1000", INDEX)
        self.assertIn("Number(serverTime) + Number(snapshotAgeSeconds)", INDEX)
        self.assertIn("snapshotAgeSeconds < -1", INDEX)
        self.assertIn("if (snapshot && statusAvailable) render(snapshot)", INDEX)
        self.assertIn("setInterval(() => {", INDEX)

    def test_unknown_command_outcome_resyncs_before_controls_reopen(self) -> None:
        self.assertIn("const idempotencyKey = crypto.randomUUID()", INDEX)
        self.assertIn("statusAvailable = false;\n    if (snapshot) render(snapshot);\n    await refreshAfterCurrent()", INDEX)
        self.assertIn("操作結果を確認できないため操作を停止しています", INDEX)
        self.assertIn("controlUnavailable = true", INDEX)
        self.assertIn("音声制御APIは利用できません", INDEX)

    def test_hidden_tab_pauses_periodic_status_polling(self) -> None:
        interval_start = INDEX.index("setInterval(() => {")
        interval_end = INDEX.index("}, 1000);", interval_start)
        interval = INDEX[interval_start:interval_end]
        self.assertIn("if (document.visibilityState !== 'visible') return", interval)
        self.assertIn("refresh();", interval)

    def test_known_offline_status_polling_backs_off_without_disabling_recovery_probes(self) -> None:
        self.assertIn("const OFFLINE_PROBE_INTERVAL_MS = 10000", INDEX)
        self.assertIn("let lastStatusAttemptAtMonotonicMs = Number.NEGATIVE_INFINITY", INDEX)
        self.assertIn("lastStatusAttemptAtMonotonicMs = performance.now()", INDEX)
        self.assertIn("function shouldPollStatus(nowMs = performance.now())", INDEX)
        self.assertIn("if (navigator.onLine !== false || statusAvailable) return true", INDEX)
        self.assertIn("nowMs - lastStatusAttemptAtMonotonicMs >= OFFLINE_PROBE_INTERVAL_MS", INDEX)
        interval_start = INDEX.index("setInterval(() => {")
        interval_end = INDEX.index("}, 1000);", interval_start)
        interval = INDEX[interval_start:interval_end]
        self.assertIn("if (!shouldPollStatus()) return", interval)
        self.assertIn("function refreshOnNetworkOnline()", INDEX)
        self.assertIn("refresh();", INDEX[INDEX.index("function refreshOnNetworkOnline()"):INDEX.index("function shouldPollStatus")])

    def test_mobile_resume_rechecks_cached_state_immediately(self) -> None:
        self.assertIn("function refreshOnResume()", INDEX)
        self.assertIn("if (document.visibilityState !== 'visible') return", INDEX)
        self.assertIn("if (navigator.onLine === false) {", INDEX)
        self.assertIn("} else if (snapshot && statusAvailable) {", INDEX)
        self.assertIn("document.addEventListener('visibilitychange', refreshOnResume)", INDEX)
        self.assertIn("window.addEventListener('pageshow', (event) =>", INDEX)
        self.assertIn("if (event.persisted) refreshOnResume()", INDEX)
        self.assertIn("if (refreshPromise) return refreshPromise", INDEX)

    def test_browser_network_events_fail_closed_and_retry_immediately(self) -> None:
        self.assertIn("function markNetworkOffline()", INDEX)
        self.assertIn(
            "markStatusUnavailable('ネットワークがオフラインです。接続復帰後に状態を再取得します')",
            INDEX,
        )
        self.assertIn("function refreshOnNetworkOnline()", INDEX)
        self.assertIn("window.addEventListener('offline', markNetworkOffline)", INDEX)
        self.assertIn("window.addEventListener('online', refreshOnNetworkOnline)", INDEX)
        self.assertIn("if (navigator.onLine === false) markNetworkOffline()", INDEX)


if __name__ == "__main__":
    unittest.main()
