from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "classify-ci-paths.py"
SPEC = importlib.util.spec_from_file_location("classify_ci_paths", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CiPathClassifierTests(unittest.TestCase):
    def test_docs_only_paths_skip_heavy_ci(self) -> None:
        self.assertFalse(
            MODULE.requires_heavy_ci(
                [
                    "docs/node-capacity-host-provenance.md\n",
                    "docs/operations/runbook.md\n",
                ]
            )
        )

    def test_runtime_path_requires_heavy_ci(self) -> None:
        self.assertTrue(
            MODULE.requires_heavy_ci(
                ["docs/overview.md\n", "apps/control-api/app.py\n"]
            )
        )

    def test_ci_and_test_changes_require_heavy_ci(self) -> None:
        for path in (
            ".github/workflows/ci.yml\n",
            "scripts/smoke-measured-soak-evidence.sh\n",
            "tests/test_example.py\n",
            "docker-compose.node.yml\n",
        ):
            with self.subTest(path=path):
                self.assertTrue(MODULE.requires_heavy_ci([path]))

    def test_empty_or_malformed_path_set_fails_closed(self) -> None:
        self.assertTrue(MODULE.requires_heavy_ci([]))
        self.assertTrue(MODULE.requires_heavy_ci(["\n", "\r\n"]))
        self.assertTrue(MODULE.requires_heavy_ci(["/docs/absolute.md\n"]))
        self.assertTrue(MODULE.requires_heavy_ci(["docs/\n"]))

    def test_deleted_or_renamed_runtime_path_is_heavy_when_diff_lists_it(self) -> None:
        # The workflow uses `git diff --no-renames`, so a rename from runtime
        # code into docs is represented by both the old and new paths.
        self.assertTrue(
            MODULE.requires_heavy_ci(
                ["apps/node-agent/relay_client.py\n", "docs/relay-client.md\n"]
            )
        )


if __name__ == "__main__":
    unittest.main()
