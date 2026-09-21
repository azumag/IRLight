from pathlib import Path
import subprocess
import sys
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
EXTRACTOR = REPO_ROOT / "scripts" / "extract-egress-stack-fingerprint.py"
SUITE = REPO_ROOT / "scripts" / "ci-docker-smoke-suite.sh"


class EgressStackFingerprintTests(unittest.TestCase):
    def run_extractor(self, payload: str) -> str:
        completed = subprocess.run(
            [sys.executable, str(EXTRACTOR)],
            input=payload,
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout.strip()

    def test_keeps_only_allowlisted_frame_metadata(self) -> None:
        secret = "rtmp://secret.example/live/stream-key-DO-NOT-LEAK"
        payload = f"""egress-gateway-1 | Current thread 0x0000ABC (most recent call first):
  File \"/app/egress.py\", line 412 in run
    destination = \"{secret}\"
  File \"/app/rtmp_sink.py\", line 95 in _poll_sink
    token = \"token-DO-NOT-LEAK\"
  File \"/usr/lib/python3.12/threading.py\", line 1002 in _bootstrap
Thread 0x0000DEF (most recent call first):
  File \"/app/egress.py\", line 701 in _shutdown_pipeline
host=secret.example arbitrary-source-text
"""
        output = self.run_extractor(payload)

        self.assertIn("egress.py:run:412", output)
        self.assertIn("rtmp_sink.py:_poll_sink:95", output)
        self.assertIn("egress.py:_shutdown_pipeline:701", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("token-DO-NOT-LEAK", output)
        self.assertNotIn("secret.example", output)
        self.assertNotIn("arbitrary-source-text", output)
        self.assertNotIn("/app/", output)
        self.assertNotIn("0000ABC", output)
        self.assertNotIn("threading.py", output)
        self.assertLessEqual(len(output.encode("utf-8")), 1024)

    def test_unrecognized_or_malformed_frames_fail_closed(self) -> None:
        payload = """Current thread 0x1 (most recent call first):
  File \"/tmp/attacker.py\", line 7 in steal_token
  File \"/app/egress.py\", line 7 in bad-function-name!
secret=still-not-copied
"""
        self.assertEqual(
            self.run_extractor(payload),
            "IRLIGHT_EGRESS_STACK_FINGERPRINT stack_fingerprint=UNAVAILABLE capped=no",
        )

    def test_frame_count_is_bounded(self) -> None:
        frames = "\n".join(
            f'  File "/app/egress.py", line {index + 1} in frame_{index}'
            for index in range(20)
        )
        output = self.run_extractor(
            f"Current thread 0x1 (most recent call first):\n{frames}\n"
        )
        self.assertIn("capped=yes", output)
        self.assertEqual(output.count("egress.py:"), 8)
        self.assertLessEqual(len(output.encode("utf-8")), 1024)

    def test_input_byte_cap_retains_late_stack_and_marks_capped(self) -> None:
        payload = (
            ("x" * (256 * 1024 + 128))
            + "\nCurrent thread 0x1 (most recent call first):\n"
            + '  File "/app/egress.py", line 412 in run\n'
        )
        output = self.run_extractor(payload)

        self.assertIn("egress.py:run:412", output)
        self.assertIn("capped=yes", output)
        self.assertLessEqual(len(output.encode("utf-8")), 1024)

    def test_input_byte_cap_discards_early_stack_and_marks_capped_unavailable(self) -> None:
        payload = (
            "Current thread 0x1 (most recent call first):\n"
            '  File "/app/egress.py", line 412 in run\n'
            + ("x" * (256 * 1024 + 128))
        )
        self.assertEqual(
            self.run_extractor(payload),
            "IRLIGHT_EGRESS_STACK_FINGERPRINT stack_fingerprint=UNAVAILABLE capped=yes",
        )

    def test_input_byte_cap_without_stack_is_capped_unavailable(self) -> None:
        payload = "x" * (256 * 1024 + 1)
        self.assertEqual(
            self.run_extractor(payload),
            "IRLIGHT_EGRESS_STACK_FINGERPRINT stack_fingerprint=UNAVAILABLE capped=yes",
        )

    def test_ci_suite_persists_fingerprint_only_for_legacy_timeout_smokes(self) -> None:
        source = SUITE.read_text(encoding="utf-8")
        self.assertIn("extract-egress-stack-fingerprint.py", source)
        self.assertIn("smoke-egress-reconnect.sh|smoke-egress-stop-terminal.sh", source)
        self.assertIn('smoke_name="${smoke##*/}"', source)
        # Preserve the canonical one-to-one legacy/rtmp2 entries used by the
        # migration-order contract; diagnostics match by basename instead.
        self.assertEqual(source.count("scripts/smoke-egress-reconnect.sh"), 1)
        self.assertEqual(source.count("scripts/smoke-egress-stop-terminal.sh"), 1)
        self.assertIn("Egress stack fingerprint (allowlisted and bounded)", source)
        self.assertIn("failure_contexts+=(", source)
        self.assertIn(
            "IRLIGHT_EGRESS_STACK_FINGERPRINT stack_fingerprint=UNAVAILABLE capped=yes",
            source,
        )
        self.assertNotIn(
            "IRLIGHT_EGRESS_STACK_FINGERPRINT stack_fingerprint=UNAVAILABLE capped=no",
            source,
        )
        self.assertNotIn("cat \"$scenario_log\"", source)


if __name__ == "__main__":
    unittest.main()
