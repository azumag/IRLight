from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-tcp-snmp-retransmits.sh"
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


def snmp_record(**overrides: int | str) -> str:
    values = {field: "0" for field in FIELDS}
    values.update({field: str(value) for field, value in overrides.items()})
    return (
        "Ip: Forwarding DefaultTTL\n"
        "Ip: 2 64\n"
        f"Tcp: {' '.join(FIELDS)}\n"
        f"Tcp: {' '.join(values[field] for field in FIELDS)}\n"
    )


class TcpSnmpRetransmitsCheckTest(unittest.TestCase):
    def _run(
        self,
        *,
        current: str | None = None,
        baseline: str | None = None,
        use_environment: bool = False,
        omit_baseline: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-tcp-snmp-") as temporary:
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

    def test_no_new_retransmits_is_ok(self) -> None:
        baseline = snmp_record(OutSegs=1000, RetransSegs=20)
        current = snmp_record(OutSegs=1100, RetransSegs=20)
        result = self._run(current=current, baseline=baseline)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_TCP_SNMP_RETRANSMITS status=OK reason=none "
            "out_segments_delta=100 retrans_segments_delta=0",
        )

    def test_retransmit_delta_is_warning(self) -> None:
        result = self._run(
            current=snmp_record(OutSegs=120, RetransSegs=6),
            baseline=snmp_record(OutSegs=100, RetransSegs=5),
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_TCP_SNMP_RETRANSMITS status=WARNING reason=tcp_retransmit_activity "
            "out_segments_delta=20 retrans_segments_delta=1",
        )

    def test_linux_maxconn_negative_one_is_accepted(self) -> None:
        baseline = snmp_record(MaxConn=-1, OutSegs=100, RetransSegs=2)
        current = snmp_record(MaxConn=-1, OutSegs=110, RetransSegs=2)
        result = self._run(current=current, baseline=baseline)
        self.assertEqual(result.returncode, 0)
        self.assertIn("out_segments_delta=10", result.stdout)

    def test_unrelated_tcp_counters_do_not_warn(self) -> None:
        result = self._run(current=snmp_record(InSegs=50, EstabResets=7))
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)

    def test_future_counter_is_allowed_when_record_is_well_formed(self) -> None:
        current = snmp_record().replace(" InCsumErrors\n", " InCsumErrors FutureCounter\n").replace(
            " 0\n", " 0 123\n", 1
        )
        result = self._run(current=current)
        self.assertEqual(result.returncode, 0)

    def test_counter_reset_is_unknown(self) -> None:
        for current, baseline in (
            (snmp_record(OutSegs=9), snmp_record(OutSegs=10)),
            (snmp_record(RetransSegs=4), snmp_record(RetransSegs=5)),
        ):
            with self.subTest(current=current[-80:]):
                result = self._run(current=current, baseline=baseline)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_TCP_SNMP_RETRANSMITS status=UNKNOWN reason=counter_reset",
                )

    def test_missing_baseline_is_unknown(self) -> None:
        result = self._run(omit_baseline=True)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_TCP_SNMP_RETRANSMITS status=UNKNOWN reason=baseline_snmp_unavailable",
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

    def test_malformed_required_counter_is_unknown(self) -> None:
        for field in ("OutSegs", "RetransSegs"):
            for value in ("-1", "nope", "9223372036854775808"):
                with self.subTest(field=field, value=value):
                    result = self._run(current=snmp_record(**{field: value}))
                    self.assertEqual(result.returncode, 3)
                    self.assertIn("reason=invalid_snmp_record", result.stdout)

    def test_environment_paths_are_supported(self) -> None:
        result = self._run(
            current=snmp_record(OutSegs=10, RetransSegs=2),
            baseline=snmp_record(OutSegs=5, RetransSegs=1),
            use_environment=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("retrans_segments_delta=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
