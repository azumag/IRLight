from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-tcp-listen-overflows.sh"
FIELDS = (
    "SyncookiesSent",
    "SyncookiesRecv",
    "SyncookiesFailed",
    "EmbryonicRsts",
    "PruneCalled",
    "RcvPruned",
    "OfoPruned",
    "OutOfWindowIcmps",
    "LockDroppedIcmps",
    "ArpFilter",
    "TW",
    "TWRecycled",
    "TWKilled",
    "PAWSActive",
    "PAWSEstab",
    "DelayedACKs",
    "DelayedACKLocked",
    "DelayedACKLost",
    "ListenOverflows",
    "ListenDrops",
    "TCPHPHits",
    "TCPPureAcks",
    "TCPHPAcks",
)


def netstat_record(**overrides: int | str) -> str:
    values = {field: "0" for field in FIELDS}
    values.update({field: str(value) for field, value in overrides.items()})
    return (
        f"TcpExt: {' '.join(FIELDS)}\n"
        f"TcpExt: {' '.join(values[field] for field in FIELDS)}\n"
        "IpExt: InNoRoutes InTruncatedPkts\n"
        "IpExt: 0 0\n"
    )


class TcpListenOverflowsCheckTest(unittest.TestCase):
    def _run(
        self,
        *,
        current: str | None = None,
        baseline: str | None = None,
        use_environment: bool = False,
        omit_baseline: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-tcp-listen-") as temporary:
            root = Path(temporary)
            current_path = root / "netstat.current"
            baseline_path = root / "netstat.baseline"
            current_path.write_text(current or netstat_record(), encoding="utf-8")
            if not omit_baseline:
                baseline_path.write_text(baseline or netstat_record(), encoding="utf-8")

            env = os.environ.copy()
            args = ["bash", str(SCRIPT)]
            if use_environment:
                env["IRLIGHT_TCP_NETSTAT_PATH"] = str(current_path)
                if not omit_baseline:
                    env["IRLIGHT_TCP_NETSTAT_BASELINE_PATH"] = str(baseline_path)
            else:
                args.append(str(current_path))
                if not omit_baseline:
                    args.append(str(baseline_path))

            return subprocess.run(args, env=env, text=True, capture_output=True, check=False)

    def test_no_new_listen_pressure_is_ok(self) -> None:
        result = self._run(
            current=netstat_record(ListenOverflows=8, ListenDrops=12),
            baseline=netstat_record(ListenOverflows=8, ListenDrops=12),
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_TCP_LISTEN_OVERFLOWS status=OK reason=none "
            "listen_overflows_delta=0 listen_drops_delta=0",
        )

    def test_listen_overflow_delta_is_warning(self) -> None:
        result = self._run(
            current=netstat_record(ListenOverflows=9, ListenDrops=12),
            baseline=netstat_record(ListenOverflows=8, ListenDrops=12),
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_TCP_LISTEN_OVERFLOWS status=WARNING reason=tcp_listener_pressure "
            "listen_overflows_delta=1 listen_drops_delta=0",
        )

    def test_listen_drop_delta_is_warning(self) -> None:
        result = self._run(
            current=netstat_record(ListenOverflows=8, ListenDrops=14),
            baseline=netstat_record(ListenOverflows=8, ListenDrops=12),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("reason=tcp_listener_pressure", result.stdout)
        self.assertIn("listen_overflows_delta=0 listen_drops_delta=2", result.stdout)

    def test_unrelated_tcp_ext_counters_do_not_warn(self) -> None:
        result = self._run(current=netstat_record(TW=50, DelayedACKs=7))
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)

    def test_future_counter_is_allowed_when_record_is_well_formed(self) -> None:
        current = netstat_record().replace(" TCPHPAcks\n", " TCPHPAcks FutureCounter\n").replace(
            " 0\nIpExt:", " 0 123\nIpExt:", 1
        )
        result = self._run(current=current)
        self.assertEqual(result.returncode, 0)

    def test_counter_reset_is_unknown(self) -> None:
        for current, baseline in (
            (netstat_record(ListenOverflows=9), netstat_record(ListenOverflows=10)),
            (netstat_record(ListenDrops=4), netstat_record(ListenDrops=5)),
        ):
            with self.subTest(current=current[-100:]):
                result = self._run(current=current, baseline=baseline)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_TCP_LISTEN_OVERFLOWS status=UNKNOWN reason=counter_reset",
                )

    def test_missing_baseline_is_unknown(self) -> None:
        result = self._run(omit_baseline=True)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_TCP_LISTEN_OVERFLOWS status=UNKNOWN reason=baseline_netstat_unavailable",
        )

    def test_missing_or_duplicate_tcp_ext_pair_is_unknown(self) -> None:
        for current in (
            "IpExt: InNoRoutes\nIpExt: 0\n",
            netstat_record() + netstat_record(),
        ):
            with self.subTest(current=current[:20]):
                result = self._run(current=current)
                self.assertEqual(result.returncode, 3)
                self.assertIn("reason=invalid_netstat_record", result.stdout)

    def test_malformed_required_counter_is_unknown(self) -> None:
        for field in ("ListenOverflows", "ListenDrops"):
            for value in ("-1", "nope", "9223372036854775808"):
                with self.subTest(field=field, value=value):
                    result = self._run(current=netstat_record(**{field: value}))
                    self.assertEqual(result.returncode, 3)
                    self.assertIn("reason=invalid_netstat_record", result.stdout)

    def test_environment_paths_are_supported(self) -> None:
        result = self._run(
            current=netstat_record(ListenOverflows=10, ListenDrops=7),
            baseline=netstat_record(ListenOverflows=9, ListenDrops=7),
            use_environment=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("listen_overflows_delta=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
