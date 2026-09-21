from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SCENARIOS = {
    "smoke-egress-rtmp2-reconnect.sh": "smoke-egress-reconnect.sh",
    "smoke-egress-rtmp2-stop-terminal.sh": "smoke-egress-stop-terminal.sh",
    "smoke-egress-rtmp2-dns-tls.sh": "smoke-egress-dns-tls.sh",
    "smoke-egress-rtmp2-publish-conflict.sh": "smoke-egress-publish-conflict.sh",
}


class Rtmp2SmokeWiringTest(unittest.TestCase):
    def test_wrappers_force_rtmp2_and_delegate_to_shared_contract(self) -> None:
        for wrapper_name, shared_name in SCENARIOS.items():
            with self.subTest(wrapper=wrapper_name):
                source = (ROOT / "scripts" / wrapper_name).read_text(encoding="utf-8")
                self.assertIn("export EGRESS_RTMP_SINK_FACTORY=rtmp2sink", source)
                self.assertIn(
                    f'exec bash "$repo_root/scripts/{shared_name}"',
                    source,
                )

    def test_shared_smokes_default_to_legacy_but_forward_selected_factory(self) -> None:
        for shared_name in SCENARIOS.values():
            with self.subTest(smoke=shared_name):
                source = (ROOT / "scripts" / shared_name).read_text(encoding="utf-8")
                self.assertIn(
                    'export EGRESS_RTMP_SINK_FACTORY="${EGRESS_RTMP_SINK_FACTORY:-rtmpsink}"',
                    source,
                )
                self.assertIn(
                    'EGRESS_RTMP_SINK_FACTORY: "${EGRESS_RTMP_SINK_FACTORY}"',
                    source,
                )

    def test_shared_docker_suite_runs_legacy_before_matching_rtmp2_probe(self) -> None:
        suite = (ROOT / "scripts" / "ci-docker-smoke-suite.sh").read_text(
            encoding="utf-8"
        )
        for wrapper_name, shared_name in SCENARIOS.items():
            with self.subTest(wrapper=wrapper_name):
                legacy_entry = f"scripts/{shared_name}"
                rtmp2_entry = f"scripts/{wrapper_name}"
                self.assertEqual(suite.count(legacy_entry), 1)
                self.assertEqual(suite.count(rtmp2_entry), 1)
                self.assertLess(suite.index(legacy_entry), suite.index(rtmp2_entry))


if __name__ == "__main__":
    unittest.main()
