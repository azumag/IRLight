from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX = REPO_ROOT / "docs/operations/README.md"


class OperationsSchemaLinksTests(unittest.TestCase):
    def test_metric_schema_links_resolve_to_repository_config(self) -> None:
        text = INDEX.read_text(encoding="utf-8")
        for name in ("operations-metric-log-schema.json", "metric-log-schema.json"):
            with self.subTest(schema=name):
                targets = re.findall(r"\]\(([^)]+)\)", text)
                matching = [target for target in targets if Path(target).name == name]
                self.assertEqual(len(matching), 1, f"expected one link to {name}")
                resolved = (INDEX.parent / matching[0]).resolve()
                self.assertEqual(resolved, REPO_ROOT / "config" / name)
                self.assertTrue(resolved.is_file(), f"missing schema: {resolved}")
                data = json.loads(resolved.read_text(encoding="utf-8"))
                self.assertEqual(data["schema_version"], 1)
                self.assertEqual(data["issue"], 11)


if __name__ == "__main__":
    unittest.main()
