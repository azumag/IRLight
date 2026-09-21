from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Each tuple is (shared smoke, expected Egress Gateway services that must receive
# the selected sink factory). DNS/TLS launches two independent Gateway services;
# the other scenarios launch one.
SCENARIOS = {
    "smoke-egress-rtmp2-reconnect.sh": ("smoke-egress-reconnect.sh", 1),
    "smoke-egress-rtmp2-stop-terminal.sh": ("smoke-egress-stop-terminal.sh", 1),
    "smoke-egress-rtmp2-dns-tls.sh": ("smoke-egress-dns-tls.sh", 2),
    "smoke-egress-rtmp2-publish-conflict.sh": ("smoke-egress-publish-conflict.sh", 1),
}


class Rtmp2SmokeWiringTest(unittest.TestCase):
    def test_wrappers_force_rtmp2_and_delegate_to_shared_contract(self) -> None:
        for wrapper_name, (shared_name, _gateway_count) in SCENARIOS.items():
            with self.subTest(wrapper=wrapper_name):
                source = (ROOT / "scripts" / wrapper_name).read_text(encoding="utf-8")
                self.assertEqual(
                    source.count("export EGRESS_RTMP_SINK_FACTORY=rtmp2sink"),
                    1,
                )
                self.assertIn(
                    f'exec bash "$repo_root/scripts/{shared_name}"',
                    source,
                )

    def test_shared_smokes_default_to_legacy_but_forward_selected_factory(self) -> None:
        pass_through = 'EGRESS_RTMP_SINK_FACTORY: "${EGRESS_RTMP_SINK_FACTORY}"'
        for shared_name, expected_gateway_count in SCENARIOS.values():
            with self.subTest(smoke=shared_name):
                source = (ROOT / "scripts" / shared_name).read_text(encoding="utf-8")
                self.assertEqual(
                    source.count(
                        'export EGRESS_RTMP_SINK_FACTORY="${EGRESS_RTMP_SINK_FACTORY:-rtmpsink}"'
                    ),
                    1,
                )
                self.assertEqual(source.count(pass_through), expected_gateway_count)

    def test_shared_docker_suite_runs_legacy_before_matching_rtmp2_probe(self) -> None:
        suite = (ROOT / "scripts" / "ci-docker-smoke-suite.sh").read_text(
            encoding="utf-8"
        )
        for wrapper_name, (shared_name, _gateway_count) in SCENARIOS.items():
            with self.subTest(wrapper=wrapper_name):
                legacy_entry = f"scripts/{shared_name}"
                rtmp2_entry = f"scripts/{wrapper_name}"
                self.assertEqual(suite.count(legacy_entry), 1)
                self.assertEqual(suite.count(rtmp2_entry), 1)
                self.assertLess(suite.index(legacy_entry), suite.index(rtmp2_entry))


if __name__ == "__main__":
    unittest.main()
