from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-egress-reconnect.sh"


class EgressReconnectEvidenceAgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        cls.helper = source.split("emit_reconnect_timeout_evidence() {", 1)[1].split(
            "\n}\n\negress_status_matches()", 1
        )[0]

    def test_rendered_counter_is_validated_before_evidence(self) -> None:
        self.assertIn('rendered_value = value.get("rendered_buffers")', self.helper)
        self.assertIn("isinstance(rendered_value, int)", self.helper)
        self.assertIn("not isinstance(rendered_value, bool)", self.helper)
        self.assertIn("rendered_value >= 0", self.helper)
        self.assertIn("rendered={rendered}", self.helper)

    def test_status_age_is_derived_and_bounded_without_raw_timestamp(self) -> None:
        self.assertIn('observed_value = value.get("observed_at")', self.helper)
        self.assertIn("math.isfinite(float(observed_value))", self.helper)
        self.assertIn("age_seconds = time.time() - float(observed_value)", self.helper)
        self.assertIn("age_seconds >= 0", self.helper)
        self.assertIn("min(999, int(age_seconds))", self.helper)
        self.assertIn("status_age_seconds={status_age_seconds}", self.helper)
        self.assertNotIn("observed_at={", self.helper)

    def test_unreadable_status_fails_closed_for_new_fields(self) -> None:
        self.assertIn(
            "connected=- rendered=- status_age_seconds=- next_retry_present=-",
            self.helper,
        )

    def test_new_evidence_does_not_expand_secret_surface(self) -> None:
        self.assertIn("json.load(sys.stdin)", self.helper)
        self.assertNotIn("$stream_key", self.helper)
        self.assertNotIn("destination_url", self.helper)
        self.assertNotIn("destination_host", self.helper)
        self.assertNotIn('printf \'%s\\n\' "$payload"', self.helper)


if __name__ == "__main__":
    unittest.main()
