from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-tcp-snmp-attempt-fails.sh"
FIELDS = (
    "RtoAlgorithm",
    "RtoMin",
    "RtoMax",
    "MaxConn",
    "ActiveOpens",
    "PassiveOpens",
    "AttemptFails",
    "EstabResets",
    "CurrEstab",
    "InSegs",
    "OutSegs",
    "RetransSegs",
    "InErrs",
    "OutRsts",
    "InCsumErrors",
)


def snmp_record(*, extra_fields: list[tuple[str, int | str]] | None = None, **overrides: int | str) -> str:
    fields = list(FIELDS)
    values = {field: "0" for field in fields}
    values.update({field: str(value) for field, value in overrides.items()})
    extra_values: list[str] = []
    if extra_fields:
        for field, value in extra_fields:
            fields.append(field)
            extra_values.append(str(value))
    base_values = [values[field] for field in FIELDS]
    return (
        "Ip: Forwarding DefaultTTL\n"
        "Ip: 2 64\n"
        f"Tcp: {' '.join(fields)}\n"
        f"Tcp: {' '.join(base_values + extra_values)}\n"
    )


class TcpSnmpAttemptFailsCheckTest(unittest.TestCase):
    def _run(
        self,
        *,
        current: str | None = None,
        baseline: str | None = None,
        use_environment: bool = False,
        omit_baseline: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-tcp-snmp-attempt-fails-") as temporary:
            root = Path(temporary)
            current_path = root / "snmp.current"
            baseline_path = root / "snmp.baseline"
            current_path.write_text(current or snmp_record(), encoding="utf-8")
            if not omit_baseline:
                baseline_path.write_text(baseline or snmp_record(), encoding="utf-8")

            env = os.environ.copy()
            args = ["bash", str(SCRIPT)]
            if use_environment:
                env["IRLIGHT_TCP_SNMP_PATH"] = str(current_path)
                if not omit_baseline:
                    env["IRLIGHT_TCP_SNMP_BASELINE_PATH"] = str(baseline_path)
            else:
                args.append(str(current_path))
                if not omit_baseline:
                    args.append(str(baseline_path))

            return subprocess.run(args, env=env, text=True, capture_output=True, check=False)

    def test_no_new_attempt_failures_is_ok(self) -> None:
        result = self._run(
            current=snmp_record(AttemptFails=15),
            baseline=snmp_record(AttemptFails=15),
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_TCP_SNMP_ATTEMPT_FAILS status=OK reason=none attempt_fails_delta=0",
        )

    def test_attempt_failure_delta_is_warning(self) -> None:
        result = self._run(
            current=snmp_record(AttemptFails=18),
            baseline=snmp_record(AttemptFails=15),
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_TCP_SNMP_ATTEMPT_FAILS status=WARNING "
            "reason=tcp_connection_attempt_failures attempt_fails_delta=3",
        )

    def test_linux_maxconn_negative_one_is_accepted(self) -> None:
        result = self._run(
            current=snmp_record(MaxConn=-1, AttemptFails=8),
            baseline=snmp_record(MaxConn=-1, AttemptFails=8),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("attempt_fails_delta=0", result.stdout)

    def test_unrelated_tcp_counters_do_not_warn(self) -> None:
        result = self._run(
            current=snmp_record(RetransSegs=50, EstabResets=20, AttemptFails=7),
            baseline=snmp_record(RetransSegs=1, EstabResets=1, AttemptFails=7),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)

    def test_future_counter_is_allowed(self) -> None:
        result = self._run(
            current=snmp_record(AttemptFails=4, extra_fields=[("FutureCounter", 123)]),
            baseline=snmp_record(AttemptFails=4, extra_fields=[("FutureCounter", 1)]),
        )
        self.assertEqual(result.returncode, 0)

    def test_duplicate_attempt_fails_is_unknown(self) -> None:
        result = self._run(
            current=snmp_record(AttemptFails=4, extra_fields=[("AttemptFails", 4)]),
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=invalid_snmp_record", result.stdout)

    def test_counter_reset_is_unknown(self) -> None:
        result = self._run(
            current=snmp_record(AttemptFails=9),
            baseline=snmp_record(AttemptFails=10),
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_TCP_SNMP_ATTEMPT_FAILS status=UNKNOWN reason=counter_reset",
        )

    def test_missing_baseline_is_unknown(self) -> None:
        result = self._run(omit_baseline=True)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_TCP_SNMP_ATTEMPT_FAILS status=UNKNOWN reason=baseline_snmp_unavailable",
        )

    def test_missing_or_duplicate_tcp_pair_is_unknown(self) -> None:
        for current in (
            "Ip: Forwarding\nIp: 2\n",
            snmp_record() + snmp_record(),
        ):
            with self.subTest(current=current[:20]):
                result = self._run(current=current)
                self.assertEqual(result.returncode, 3)
                self.assertIn("reason=invalid_snmp_record", result.stdout)

    def test_malformed_attempt_fails_is_unknown(self) -> None:
        for value in ("-1", "nope", "9223372036854775808"):
            with self.subTest(value=value):
                result = self._run(current=snmp_record(AttemptFails=value))
                self.assertEqual(result.returncode, 3)
                self.assertIn("reason=invalid_snmp_record", result.stdout)

    def test_environment_paths_are_supported(self) -> None:
        result = self._run(
            current=snmp_record(AttemptFails=10),
            baseline=snmp_record(AttemptFails=9),
            use_environment=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("attempt_fails_delta=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
