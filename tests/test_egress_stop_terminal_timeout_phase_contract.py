from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-egress-stop-terminal.sh"

EXPECTED_PHASES = (
    "compose-config",
    "compose-up",
    "initial-connected",
    "target-stop",
    "reconnecting",
    "backoff-window",
    "gateway-stop",
    "stopped-user-stopped",
    "target-recovery-start",
    "target-recovery-observe",
    "target-recovery-stopped-status",
    "unsafe-destination-terminal",
    "unsafe-destination-failed",
    "unsafe-destination-reason",
    "secret-redaction-terminal-output",
    "secret-redaction-logs-read",
    "secret-redaction-logs",
)


class EgressStopTerminalTimeoutPhaseContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_major_blocking_boundaries_emit_fixed_phase_markers(self) -> None:
        emitted = tuple(re.findall(r'^emit_phase "([^"]+)"$', self.source, re.MULTILINE))
        self.assertEqual(emitted, EXPECTED_PHASES)
        for phase in emitted:
            self.assertRegex(phase, r"^[A-Za-z0-9._-]+$")

        self.assertIn("IRLIGHT_DOCKER_SMOKE_PHASE phase=%s", self.source)
        self.assertIn(
            'emit_phase "compose-up"\nif ! "${compose[@]}" up -d --build; then',
            self.source,
        )
        self.assertIn(
            'emit_phase "reconnecting"\nif ! wait_egress_status RECONNECTING 45; then',
            self.source,
        )
        self.assertIn(
            'emit_phase "gateway-stop"\nif ! "${compose[@]}" stop -t 5 egress-gateway',
            self.source,
        )
        self.assertIn(
            'emit_phase "target-recovery-start"\nif ! "${compose[@]}" start egress-target',
            self.source,
        )
        self.assertIn(
            'emit_phase "unsafe-destination-terminal"\nset +e\nterminal_output=',
            self.source,
        )

    def test_phase_allowlist_is_hard_coded_and_matches_emitted_tokens(self) -> None:
        case_match = re.search(
            r'case "\$phase" in\n\s+([^\n]+)\)\n\s+;;',
            self.source,
        )
        self.assertIsNotNone(case_match)
        assert case_match is not None
        allowed = tuple(case_match.group(1).split("|"))
        self.assertEqual(allowed, EXPECTED_PHASES)
        self.assertNotIn("$", case_match.group(1))
        self.assertNotIn("/", case_match.group(1))

    def test_outer_timeout_contract_remains_unchanged(self) -> None:
        # This smoke only emits diagnostics. The suite-level 180s bound and the
        # reconnect/stop timing contracts must remain owned by their existing code.
        self.assertIn("wait_egress_status CONNECTED 60", self.source)
        self.assertIn("wait_egress_status RECONNECTING 45", self.source)
        self.assertIn('"${compose[@]}" stop -t 5 egress-gateway', self.source)
        self.assertIn("wait_egress_status STOPPED 10", self.source)
        self.assertNotIn("smoke_timeout_seconds=", self.source)


if __name__ == "__main__":
    unittest.main()
