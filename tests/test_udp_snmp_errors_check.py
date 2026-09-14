from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-udp-snmp-errors.sh"
FIELDS = (
    "InDatagrams",
    "NoPorts",
    "InErrors",
    "OutDatagrams",
    "RcvbufErrors",
    "SndbufErrors",
    "InCsumErrors",
    "IgnoredMulti",
    "MemErrors",
)


def snmp_record(**overrides: int | str) -> str:
    values = {field: "0" for field in FIELDS}
    values.update({field: str(value) for field, value in overrides.items()})
    return (
        "Ip: Forwarding DefaultTTL\n"
        "Ip: 2 64\n"
        f"Udp: {' '.join(FIELDS)}\n"
        f"Udp: {' '.join(values[field] for field in FIELDS)}\n"
    )


class UdpSnmpErrorsCheckTest(unittest.TestCase):
    def _run(
        self,
        *,
        current: str | None = None,
        baseline: str | None = None,
        use_environment: bool = False,
        omit_baseline: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-udp-snmp-") as temporary:
            root = Path(temporary)
            current_path = root / "snmp.current"
            baseline_path = root / "snmp.baseline"
            current_path.write_text(current or snmp_record(), encoding="utf-8")
            if not omit_baseline:
                baseline_path.write_text(baseline or snmp_record(), encoding="utf-8")

            env = os.environ.copy()
            args = ["bash", str(SCRIPT)]
            if use_environment:
                env["IRLIGHT_UDP_SNMP_PATH"] = str(current_path)
                if not omit_baseline:
                    env["IRLIGHT_UDP_SNMP_BASELINE_PATH"] = str(baseline_path)
            else:
                args.append(str(current_path))
                if not omit_baseline:
                    args.append(str(baseline_path))

            return subprocess.run(args, env=env, text=True, capture_output=True, check=False)

    def test_no_new_errors_is_ok(self) -> None:
        baseline = snmp_record(InErrors=2, RcvbufErrors=3, SndbufErrors=4, InCsumErrors=5)
        result = self._run(current=baseline, baseline=baseline)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_UDP_SNMP_ERRORS status=OK reason=none in_errors_delta=0 rcvbuf_errors_delta=0 sndbuf_errors_delta=0 csum_errors_delta=0",
        )

    def test_each_error_counter_delta_is_warning(self) -> None:
        for field, output_name in (
            ("InErrors", "in_errors_delta"),
            ("RcvbufErrors", "rcvbuf_errors_delta"),
            ("SndbufErrors", "sndbuf_errors_delta"),
            ("InCsumErrors", "csum_errors_delta"),
        ):
            with self.subTest(field=field):
                result = self._run(current=snmp_record(**{field: 1}))
                self.assertEqual(result.returncode, 1)
                self.assertIn("status=WARNING reason=udp_error_activity", result.stdout)
                self.assertIn(f"{output_name}=1", result.stdout)

    def test_unrelated_udp_counters_do_not_warn(self) -> None:
        result = self._run(current=snmp_record(InDatagrams=50, OutDatagrams=60, NoPorts=7))
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)

    def test_future_counter_is_allowed_when_record_is_well_formed(self) -> None:
        current = snmp_record().replace(" MemErrors\n", " MemErrors FutureCounter\n").replace(
            " 0\n", " 0 123\n", 1
        )
        result = self._run(current=current)
        self.assertEqual(result.returncode, 0)

    def test_counter_reset_is_unknown(self) -> None:
        result = self._run(current=snmp_record(InErrors=4), baseline=snmp_record(InErrors=5))
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_UDP_SNMP_ERRORS status=UNKNOWN reason=counter_reset",
        )

    def test_missing_baseline_is_unknown(self) -> None:
        result = self._run(omit_baseline=True)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_UDP_SNMP_ERRORS status=UNKNOWN reason=baseline_snmp_unavailable",
        )

    def test_missing_or_duplicate_udp_pair_is_unknown(self) -> None:
        for current in (
            "Ip: Forwarding\nIp: 2\n",
            snmp_record() + snmp_record(),
        ):
            with self.subTest(current=current[:20]):
                result = self._run(current=current)
                self.assertEqual(result.returncode, 3)
                self.assertIn("reason=invalid_snmp_record", result.stdout)

    def test_malformed_required_counter_is_unknown(self) -> None:
        for value in ("-1", "nope", "9223372036854775808"):
            with self.subTest(value=value):
                result = self._run(current=snmp_record(InErrors=value))
                self.assertEqual(result.returncode, 3)
                self.assertIn("reason=invalid_snmp_record", result.stdout)

    def test_environment_paths_are_supported(self) -> None:
        result = self._run(current=snmp_record(RcvbufErrors=1), use_environment=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("rcvbuf_errors_delta=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
