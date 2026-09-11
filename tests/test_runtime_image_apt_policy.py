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


if __name__ == "__main__":
    unittest.main()
