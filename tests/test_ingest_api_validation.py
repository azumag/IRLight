from __future__ import annotations

import sys
import unittest
from pathlib import Path

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from ingest_api import ACCEPTING_INGEST_STATES, IssueIngestCredentialRequest  # noqa: E402


class IngestApiValidationTest(unittest.TestCase):
    def test_protocol_list_must_not_be_empty(self) -> None:
        with self.assertRaises(ValidationError):
            IssueIngestCredentialRequest(protocols=[])

    def test_unknown_protocol_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            IssueIngestCredentialRequest(protocols=["rtsp"])

    def test_degraded_session_remains_eligible_for_ingest_auth(self) -> None:
        self.assertIn("DEGRADED", ACCEPTING_INGEST_STATES)


if __name__ == "__main__":
    unittest.main()
