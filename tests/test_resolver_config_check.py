import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-resolver-config.sh"


class ResolverConfigCheckTest(unittest.TestCase):
    def run_check(self, content=None, *, env=None, missing=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "resolv.conf"
            if not missing:
                path.write_text(content or "", encoding="ascii")

            merged_env = os.environ.copy()
            if env:
                merged_env.update(env)
            return subprocess.run(
                ["bash", str(SCRIPT), str(path)],
                text=True,
                capture_output=True,
                check=False,
                env=merged_env,
            )

    def test_ipv4_nameserver_is_ok_without_echoing_address(self):
        result = self.run_check("nameserver 203.0.113.9\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_RESOLVER_CONFIG_HEALTH status=OK nameserver_count=1",
        )
        self.assertNotIn("203.0.113.9", result.stdout)

    def test_ipv6_nameserver_is_ok(self):
        result = self.run_check("nameserver 2001:4860:4860::8888\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("nameserver_count=1", result.stdout)

    def test_scoped_ipv6_nameserver_is_ok(self):
        result = self.run_check("nameserver fe80::1%eth0\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)

    def test_comments_and_non_nameserver_directives_are_ignored(self):
        result = self.run_check(
            "# generated\nsearch example.invalid\noptions timeout:2 attempts:2\n"
            "nameserver 192.0.2.53 # local resolver\n"
            "; another comment\nnameserver 2001:db8::53\n"
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("nameserver_count=2", result.stdout)

    def test_missing_nameserver_is_critical(self):
        result = self.run_check("search example.invalid\noptions timeout:2\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("reason=nameserver_missing", result.stdout)

    def test_missing_file_is_unknown(self):
        result = self.run_check(missing=True)
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("reason=resolver_config_unavailable", result.stdout)

    def test_missing_nameserver_value_is_unknown(self):
        result = self.run_check("nameserver\n")
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=invalid_nameserver_record", result.stdout)

    def test_extra_nameserver_fields_are_unknown(self):
        result = self.run_check("nameserver 192.0.2.53 unexpected\n")
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=invalid_nameserver_record", result.stdout)

    def test_hostname_is_not_accepted_as_nameserver_literal(self):
        result = self.run_check("nameserver resolver.example.invalid\n")
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=invalid_nameserver_address", result.stdout)

    def test_malformed_ipv6_is_unknown(self):
        for address in ("::::", "2001:db8:1:2:3:4:5", "2001:db8::1::2"):
            with self.subTest(address=address):
                result = self.run_check(f"nameserver {address}\n")
                self.assertEqual(result.returncode, 3)
                self.assertIn("reason=invalid_nameserver_address", result.stdout)

    def test_malformed_ipv4_is_unknown(self):
        for address in ("999.1.1.1", "1.2.3.4.", "1..2.3"):
            with self.subTest(address=address):
                result = self.run_check(f"nameserver {address}\n")
                self.assertEqual(result.returncode, 3)
                self.assertIn("reason=invalid_nameserver_address", result.stdout)

    def test_environment_path_override_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom-resolver.conf"
            path.write_text("nameserver 192.0.2.53\n", encoding="ascii")
            env = os.environ.copy()
            env["IRLIGHT_RESOLVER_CONFIG_PATH"] = str(path)
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
        self.assertEqual(result.returncode, 0)
        self.assertIn("nameserver_count=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
