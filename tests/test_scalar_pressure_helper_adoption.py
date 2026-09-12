from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCALAR_CHECKS = (
    "scripts/check-cgroup-pid-pressure.sh",
    "scripts/check-cgroup-memory-pressure.sh",
    "scripts/check-conntrack-pressure.sh",
    "scripts/check-file-handle-pressure.sh",
    "scripts/check-task-pressure.sh",
    "scripts/check-process-fd-pressure.sh",
)


class ScalarPressureHelperAdoptionTest(unittest.TestCase):
    def test_scalar_checks_share_validation_helper(self) -> None:
        for relative_path in SCALAR_CHECKS:
            with self.subTest(script=relative_path):
                source = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn('lib/scalar-pressure-common.sh', source)
                self.assertNotIn("\nis_uint() {", source)
                self.assertNotIn("\nnormalize_int64_uint() {", source)

    def test_shared_helper_stays_policy_neutral(self) -> None:
        source = (ROOT / "scripts/lib/scalar-pressure-common.sh").read_text(
            encoding="utf-8"
        )
        for policy_token in (
            "NO_HEADROOM",
            "OVER_LIMIT",
            "conntrack",
            "pids.max",
            "memory.max",
            "Max open files",
        ):
            with self.subTest(token=policy_token):
                self.assertNotIn(policy_token, source)


if __name__ == "__main__":
    unittest.main()
