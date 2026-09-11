from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DOCKERFILES = (
    ROOT / "apps" / "continuity" / "Dockerfile",
    ROOT / "apps" / "control-api" / "Dockerfile",
    ROOT / "apps" / "node-agent" / "Dockerfile",
)


class RuntimeImageAptPolicyTest(unittest.TestCase):
    def test_runtime_images_bound_package_fetches(self) -> None:
        for dockerfile in RUNTIME_DOCKERFILES:
            with self.subTest(dockerfile=str(dockerfile.relative_to(ROOT))):
                text = dockerfile.read_text(encoding="utf-8")
                self.assertIn("APT::Update::Error-Mode=any", text)
                self.assertIn("Acquire::Retries=4", text)
                self.assertIn("Acquire::https::Timeout=10", text)
                self.assertIn(
                    "timeout --signal=TERM --kill-after=10s 120s",
                    text,
                )
                self.assertIn(
                    "timeout --signal=TERM --kill-after=10s 600s",
                    text,
                )

    def test_continuity_restarts_a_timed_out_index_refresh_but_stays_bounded(self) -> None:
        text = (ROOT / "apps" / "continuity" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("update-attempts=3", text)
        self.assertIn("update_attempt=1", text)
        self.assertIn('if [ "${update_attempt}" -ge 3 ]', text)
        self.assertIn("exit \"${update_status}\"", text)


if __name__ == "__main__":
    unittest.main()
