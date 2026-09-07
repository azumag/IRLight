from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "apps" / "control-api" / "static" / "index.html").read_text(
    encoding="utf-8"
)


class ControlUiAccessibilityContractTest(unittest.TestCase):
    def test_polling_grid_is_not_a_live_region(self) -> None:
        self.assertIn('<section class="card grid" aria-labelledby="statusRegionHeading">', INDEX)
        self.assertNotIn('class="card grid" aria-live="polite"', INDEX)

    def test_major_sections_have_accessible_landmark_names(self) -> None:
        for heading_id, heading, section_class in (
            ("statusRegionHeading", "配信状態", "card grid"),
            ("audioControlHeading", "配信音声の操作", "card"),
            ("phase0NoticeHeading", "Phase 0 ローカルPoCの注意事項", "card small"),
        ):
            with self.subTest(heading_id=heading_id):
                self.assertIn(
                    f'<h2 id="{heading_id}" class="sr-only">{heading}</h2>', INDEX
                )
                self.assertIn(
                    f'<section class="{section_class}" aria-labelledby="{heading_id}">',
                    INDEX,
                )

    def test_dedicated_status_live_region_is_atomic_and_polite(self) -> None:
        self.assertIn(
            'id="statusAnnouncement" class="sr-only" role="status" aria-live="polite" aria-atomic="true"',
            INDEX,
        )
        self.assertIn("let lastAnnouncedStatus = null", INDEX)
        self.assertIn("if (message === lastAnnouncedStatus) return", INDEX)
        self.assertIn("$('statusAnnouncement').textContent = message", INDEX)

    def test_status_announcement_uses_semantic_state_not_poll_timestamp(self) -> None:
        self.assertIn(
            "announceStatus(runtimeTrusted ? `セッション ${sessionLabel}。配信音声 ${state.label}` : '配信状態を確認できません')",
            INDEX,
        )
        self.assertNotIn("announceStatus($('updated').textContent)", INDEX)

    def test_repeated_poll_failures_do_not_rewrite_identical_alert_text(self) -> None:
        self.assertIn(
            "if ($('error').textContent !== message) $('error').textContent = message",
            INDEX,
        )

    def test_polling_recovery_preserves_action_feedback(self) -> None:
        self.assertIn("let statusErrorMessage = ''", INDEX)
        self.assertIn("let actionErrorMessage = ''", INDEX)
        self.assertIn("const message = [statusErrorMessage, actionErrorMessage].filter(Boolean).join(' / ')", INDEX)
        self.assertIn("setStatusError(message)", INDEX)
        self.assertIn("clearStatusError()", INDEX)
        self.assertIn("setActionError('別画面で状態が更新されました。最新状態を再取得しました')", INDEX)
        self.assertNotIn("if ($('error').textContent) $('error').textContent = ''", INDEX)

    def test_action_feedback_clears_when_a_new_action_starts(self) -> None:
        self.assertIn("clearActionError();\n  applying = true;", INDEX)
        self.assertIn("setActionError(`音声切替に失敗: ${error.message}。${recovery}`)", INDEX)

    def test_status_grid_can_shrink_on_narrow_mobile_viewports(self) -> None:
        self.assertIn(
            ".value { min-width: 0; font-weight: 750; text-align: right; overflow-wrap: anywhere; }",
            INDEX,
        )
        self.assertIn(".badge { max-width: 100%;", INDEX)
        self.assertIn(".badge > span:last-child { min-width: 0; overflow-wrap: anywhere; }", INDEX)
        self.assertIn(
            "@media (max-width: 24rem) { .grid { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); } }",
            INDEX,
        )

    def test_control_connection_scope_is_explained_in_the_ui(self) -> None:
        self.assertIn(
            'id="connection" class="value" aria-describedby="connectionHelp"',
            INDEX,
        )
        self.assertIn(
            '<p id="connectionHelp" class="grid-note small">「管理接続」はこの画面と Control API の通信状態です。入力映像・配信音声・配信先の接続状態とは別に確認してください。</p>',
            INDEX,
        )
        self.assertIn('.grid-note { grid-column: 1 / -1; margin: .15rem 0 0; }', INDEX)

    def test_compact_mobile_status_summary_stays_visible_without_duplicate_accessible_output(self) -> None:
        self.assertIn(
            ".status-summary { position: sticky; top: max(.5rem, env(safe-area-inset-top));",
            INDEX,
        )
        self.assertIn('<div class="status-summary" aria-hidden="true">', INDEX)
        self.assertIn('id="stickySession" class="status-summary-value"', INDEX)
        self.assertIn('id="stickyAudio" class="status-summary-value"', INDEX)
        self.assertIn('id="stickyConnection" class="status-summary-value"', INDEX)
        self.assertIn('id="stickyChecked" class="status-summary-value"', INDEX)
        self.assertIn("$('stickySession').textContent = unknown", INDEX)
        self.assertIn("$('stickyAudio').textContent = unknown", INDEX)
        self.assertIn("$('stickyConnection').textContent = '再接続中…'", INDEX)
        self.assertIn("$('stickyChecked').textContent = lastCheckedText()", INDEX)
        self.assertIn("$('stickySession').textContent = sessionLabel", INDEX)
        self.assertIn("$('stickyAudio').textContent = state.label", INDEX)
        self.assertIn("$('stickyConnection').textContent = '接続済み'", INDEX)

    def test_manual_status_retry_reports_busy_state_without_duplicate_requests(self) -> None:
        self.assertIn("function syncStatusRetryBusyState()", INDEX)
        self.assertIn("button.disabled = busy", INDEX)
        self.assertIn("button.textContent = busy ? '再確認中…' : '今すぐ再確認'", INDEX)
        self.assertIn("button.setAttribute('aria-busy', 'true')", INDEX)
        self.assertIn("button.removeAttribute('aria-busy')", INDEX)
        self.assertIn("if (refreshPromise) return refreshPromise", INDEX)

    def test_primary_audio_action_has_visible_keyboard_focus(self) -> None:
        self.assertIn(
            "button:focus-visible { outline: 3px solid #f8fafc; outline-offset: 3px; box-shadow: 0 0 0 2px #0b1020; }",
            INDEX,
        )
        self.assertIn("@media (forced-colors: active)", INDEX)
        self.assertIn("button:focus-visible { outline-color: CanvasText; box-shadow: none; }", INDEX)

    def test_audio_action_progress_is_announced_without_reusing_polling_status(self) -> None:
        self.assertIn(
            'id="actionAnnouncement" class="sr-only" role="status" aria-live="polite" aria-atomic="true"',
            INDEX,
        )
        self.assertIn("$('actionAnnouncement').textContent = message", INDEX)
        self.assertIn("function clearActionAnnouncement()", INDEX)
        self.assertIn("$('actionAnnouncement').textContent = ''", INDEX)
        self.assertIn("applying = false;\n    clearActionAnnouncement();", INDEX)
        self.assertIn("function syncActionBusyState(button)", INDEX)
        self.assertIn("button.setAttribute('aria-busy', 'true')", INDEX)
        self.assertIn("button.removeAttribute('aria-busy')", INDEX)
        self.assertGreaterEqual(INDEX.count("syncActionBusyState(button)"), 3)
        self.assertIn("配信音声のミュート指示を送信中です", INDEX)
        self.assertIn("配信音声のミュート解除指示を送信中です", INDEX)

    def test_primary_audio_action_exposes_toggle_semantics_only_for_trusted_state(self) -> None:
        self.assertIn(
            'id="audioButton" aria-label="配信音声をミュート" aria-describedby="audioButtonHelp" disabled',
            INDEX,
        )
        self.assertIn('id="audioButtonHelp" class="small"', INDEX)
        self.assertNotIn("button.setAttribute('aria-label'", INDEX)
        self.assertIn("button.removeAttribute('aria-pressed')", INDEX)
        self.assertIn(
            "button.setAttribute('aria-pressed', state.desired === 'MUTED' ? 'true' : 'false')",
            INDEX,
        )
        self.assertIn(
            "if (state.runtimeStale || !state.actualKnown || !state.commandAcked)",
            INDEX,
        )


if __name__ == "__main__":
    unittest.main()
