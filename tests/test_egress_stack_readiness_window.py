from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def function_body(script: str, name: str) -> str:
    marker = f"{name}() {{\n"
    start = script.index(marker) + len(marker)
    end = script.index("\n}\n", start)
    return script[start:end]


class EgressStackReadinessWindowTests(unittest.TestCase):
    def assert_durable_redacted_readiness_check(self, relative_path: str, redactor: str) -> None:
        script = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        body = function_body(script, "request_egress_stack_dump")

        self.assertIn("logs --no-color egress-gateway", body)
        self.assertNotIn("--tail=", body)
        self.assertIn(f"| {redactor} || true", body)
        self.assertIn("IRLIGHT_EGRESS_STACK_SIGNAL_READY signal=SIGUSR2", body)
        self.assertIn("reason=handler-unconfirmed", body)
        self.assertIn("kill -s SIGUSR2 egress-gateway", body)

    def test_reconnect_readiness_marker_cannot_age_out_of_tail_window(self) -> None:
        self.assert_durable_redacted_readiness_check(
            "scripts/smoke-egress-reconnect.sh",
            "redact_stream_key",
        )

    def test_stop_terminal_readiness_marker_cannot_age_out_of_tail_window(self) -> None:
        self.assert_durable_redacted_readiness_check(
            "scripts/smoke-egress-stop-terminal.sh",
            "redact_generated_secrets",
        )


if __name__ == "__main__":
    unittest.main()
