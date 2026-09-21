from __future__ import annotations

import io
import signal
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EGRESS_DIR = ROOT / "apps" / "egress-gateway"
sys.path.insert(0, str(EGRESS_DIR))

from stack_signal import STACK_SIGNAL_NAME, install_stack_signal_handler  # noqa: E402


class EgressStackSignalDiagnosticsTest(unittest.TestCase):
    def test_installs_all_thread_faulthandler_without_chaining(self) -> None:
        calls: list[tuple[int, dict[str, object]]] = []
        output = io.StringIO()

        def register(signum: int, **kwargs: object) -> None:
            calls.append((signum, kwargs))

        installed = install_stack_signal_handler(register=register, output=output)
        self.assertTrue(installed)
        self.assertEqual(STACK_SIGNAL_NAME, "SIGUSR2")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], signal.SIGUSR2)
        self.assertIs(calls[0][1]["file"], output)
        self.assertIs(calls[0][1]["all_threads"], True)
        self.assertIs(calls[0][1]["chain"], False)

    def test_registration_failure_is_non_fatal(self) -> None:
        def register(_signum: int, **_kwargs: object) -> None:
            raise RuntimeError("synthetic registration failure")

        self.assertFalse(install_stack_signal_handler(register=register, output=io.StringIO()))

    def test_entrypoint_keeps_signal_diagnostics_opt_in(self) -> None:
        source = (EGRESS_DIR / "egress_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn("EGRESS_STACK_SIGNAL_DIAGNOSTICS", source)
        self.assertIn('os.getenv(_STACK_SIGNAL_DIAGNOSTICS_ENV) != "1"', source)
        self.assertIn("install_stack_signal_handler()", source)
        self.assertIn("IRLIGHT_EGRESS_STACK_SIGNAL_READY signal=", source)
        main_body = source.split("def main() -> int:", 1)[1]
        self.assertLess(
            main_body.index("_install_stack_diagnostics()"),
            main_body.index("return egress.main()"),
        )

    def test_container_packages_signal_helper(self) -> None:
        dockerfile = (EGRESS_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("stack_signal.py", dockerfile)

    def test_reconnect_smoke_signals_only_after_timeout_and_ready_marker(self) -> None:
        smoke = (ROOT / "scripts" / "smoke-egress-reconnect.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('EGRESS_STACK_SIGNAL_DIAGNOSTICS: "1"', smoke)
        helper = smoke.split("request_egress_stack_dump() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("IRLIGHT_EGRESS_STACK_SIGNAL_READY signal=SIGUSR2", helper)
        self.assertIn("kill -s SIGUSR2 egress-gateway", helper)
        self.assertLess(
            helper.index("IRLIGHT_EGRESS_STACK_SIGNAL_READY signal=SIGUSR2"),
            helper.index("kill -s SIGUSR2 egress-gateway"),
        )
        self.assertNotIn("stream_key_file", helper)
        self.assertNotIn("secret_file", helper)

        timeout_block = smoke.split(
            "if ! wait_egress_status RECONNECTING 45; then", 1
        )[1].split("fi", 1)[0]
        self.assertIn("request_egress_stack_dump", timeout_block)
        self.assertIn("emit_reconnect_timeout_evidence", timeout_block)
        self.assertLess(
            timeout_block.index("request_egress_stack_dump"),
            timeout_block.index("emit_reconnect_timeout_evidence"),
        )


if __name__ == "__main__":
    unittest.main()
