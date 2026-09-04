from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "scripts" / "smoke-compose.sh"


class SmokeComposeOrderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SMOKE.read_text(encoding="utf-8")

    def test_session_first_node_auth_then_srt_verify(self) -> None:
        setup_call = self.source.index(
            'current_stage="authenticated-ingest-setup"\nsetup_control_plane_and_ingest'
        )
        node_up = self.source.index('current_stage="node-up"', setup_call)
        auth_ready = self.source.index(
            'current_stage="node-auth-ready"\nwait_node_registered 45\nwait_ingest_auth_proxy_ready 30',
            node_up,
        )
        srt_verify = self.source.index(
            'current_stage="authenticated-srt-verify"\nverify_srt_destination',
            auth_ready,
        )
        initial_holding = self.source.index('current_stage="initial-holding"', srt_verify)

        self.assertLess(setup_call, node_up)
        self.assertLess(node_up, auth_ready)
        self.assertLess(auth_ready, srt_verify)
        self.assertLess(srt_verify, initial_holding)

    def test_setup_only_creates_srt_destination(self) -> None:
        setup_body = self.source.split("setup_control_plane_and_ingest() {", 1)[1].split(
            "\n}\n\nverify_srt_destination()", 1
        )[0]
        self.assertIn("ingest_srt_destination_id=", setup_body)
        self.assertNotIn(
            "$base_url/v1/destinations/$ingest_srt_destination_id/verify", setup_body
        )

    def test_proxy_readiness_uses_real_node_local_auth_endpoint(self) -> None:
        readiness_body = self.source.split("wait_ingest_auth_proxy_ready() {", 1)[1].split(
            "\n}\n\nwait_node_ingest_status()", 1
        )[0]
        self.assertIn("http://127.0.0.1:8090/auth", readiness_body)
        self.assertIn('if [[ "$status" == "200" ]]', readiness_body)
        self.assertNotIn("AUTH_SECRET=", readiness_body)


if __name__ == "__main__":
    unittest.main()
