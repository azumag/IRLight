from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "apps" / "control-api" / "static" / "index.html").read_text(
    encoding="utf-8"
)


class ControlUiAccessibilityContractTest(unittest.TestCase):
    def test_polling_grid_is_not_a_live_region(self) -> None:
        self.assertIn('<section class="card grid">', INDEX)
        self.assertNotIn('<section class="card grid" aria-live="polite">', INDEX)

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


if __name__ == "__main__":
    unittest.main()
