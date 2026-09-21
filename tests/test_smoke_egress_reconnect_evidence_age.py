import json
from pathlib import Path
import subprocess
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-egress-reconnect.sh"


class EgressReconnectEvidenceAgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        cls.helper = source.split("emit_reconnect_timeout_evidence() {", 1)[1].split(
            "\n}\n\negress_status_matches()", 1
        )[0]
        cls.program = cls.helper.split("python3 -c '\n", 1)[1].split(
            "\n' \"$gateway_state\"", 1
        )[0]

    def run_program(self, payload: object, gateway: str = "running") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", self.program, gateway],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def fields(output: str) -> dict[str, str]:
        tokens = output.strip().split()
        if not tokens or tokens[0] != "IRLIGHT_EGRESS_RECONNECT_EVIDENCE":
            raise AssertionError(output)
        return dict(token.split("=", 1) for token in tokens[1:])

    def test_rendered_counter_and_status_age_are_emitted_from_valid_status(self) -> None:
        observed_at = time.time() - 42
        result = self.run_program(
            {
                "status": "CONNECTED",
                "reason_code": None,
                "attempt": 1,
                "connected": True,
                "rendered_buffers": 123,
                "observed_at": observed_at,
                "next_retry_at": None,
                "destination_host": "secret.example",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = self.fields(result.stdout)
        self.assertEqual(fields["status"], "CONNECTED")
        self.assertEqual(fields["rendered"], "123")
        self.assertGreaterEqual(int(fields["status_age_seconds"]), 40)
        self.assertLessEqual(int(fields["status_age_seconds"]), 60)
        self.assertEqual(fields["gateway"], "running")
        self.assertNotIn("secret.example", result.stdout)
        self.assertNotIn(str(observed_at), result.stdout)

    def test_status_age_is_bounded(self) -> None:
        result = self.run_program(
            {
                "status": "CONNECTED",
                "attempt": 1,
                "connected": True,
                "rendered_buffers": 2,
                "observed_at": time.time() - 5000,
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fields(result.stdout)["status_age_seconds"], "999")

    def test_malformed_and_future_progress_fields_fail_closed(self) -> None:
        result = self.run_program(
            {
                "status": "CONNECTED",
                "attempt": 1,
                "connected": True,
                "rendered_buffers": True,
                "observed_at": time.time() + 60,
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = self.fields(result.stdout)
        self.assertEqual(fields["rendered"], "-")
        self.assertEqual(fields["status_age_seconds"], "-")

    def test_source_keeps_new_fields_fail_closed_and_secret_safe(self) -> None:
        self.assertIn("isinstance(rendered_value, int)", self.helper)
        self.assertIn("not isinstance(rendered_value, bool)", self.helper)
        self.assertIn("math.isfinite(float(observed_value))", self.helper)
        self.assertIn("min(999, int(age_seconds))", self.helper)
        self.assertIn(
            "connected=- rendered=- status_age_seconds=- next_retry_present=-",
            self.helper,
        )
        self.assertNotIn("$stream_key", self.helper)
        self.assertNotIn("destination_url", self.helper)
        self.assertNotIn("destination_host", self.helper)
        self.assertNotIn('printf \'%s\\n\' "$payload"', self.helper)


if __name__ == "__main__":
    unittest.main()
