from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "operations" / "filesystem-readonly-monitoring.md"
INDEX = ROOT / "docs" / "operations" / "README.md"
SCRIPT = ROOT / "scripts" / "check-filesystem-readonly.py"


class FilesystemReadonlyRunbookContractTest(unittest.TestCase):
    def test_operations_index_links_runbook_exactly_once(self) -> None:
        index = INDEX.read_text(encoding="utf-8")
        self.assertEqual(
            index.count("](filesystem-readonly-monitoring.md)"),
            1,
        )

    def test_runbook_keeps_read_only_fail_closed_contract(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        for marker in (
            "os.statvfs()",
            "ST_RDONLY",
            "status=CRITICAL reason=filesystem_read_only",
            "status=UNKNOWN reason=path_unavailable",
            "probe file",
            "mount/remount",
            "対象 path や内部例外文字列を反射しない",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

    def test_script_uses_metadata_only_without_write_probe_apis(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("os.statvfs(path)", source)
        for forbidden in (
            "os.open(",
            ".write_text(",
            ".touch(",
            ".unlink(",
            ".mkdir(",
            "tempfile",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
