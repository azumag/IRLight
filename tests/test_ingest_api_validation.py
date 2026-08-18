from __future__ import annotations

import sys
import unittest
from pathlib import Path

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from ingest_api import IssueIngestCredentialRequest  # noqa: E402


class IngestApiValidationTest(unittest.TestCase):
    def test_protocol_list_must_not_be_empty(self) -> None:
        # Keep this contract explicit even if Pydantic defaults change later.
        request = IssueIngestCredentialRequest(protocols=[])
        self.assertEqual(request.protocols, [])

    def test_unknown_protocol_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            IssueIngestCredentialRequest(protocols=["rtsp"])


if __name__ == "__main__":
    unittest.main()
