from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "check-rtmps-certificate.sh"


class RtmpsCertificateCheckTests(unittest.TestCase):
    def _run(
        self,
        *,
        enddate_exit: int = 0,
        checkend_exit: int = 0,
        args: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        create_cert: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            openssl = bin_dir / "openssl"
            openssl.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    if [[ "$*" == *" -enddate"* ]]; then
                      if [[ {enddate_exit} -ne 0 ]]; then exit {enddate_exit}; fi
                      printf '%s\\n' 'notAfter=Oct  1 12:00:00 2026 GMT'
                      exit 0
                    fi
                    if [[ "$*" == *" -checkend "* ]]; then
                      exit {checkend_exit}
                    fi
                    exit 9
                    """
                ),
                encoding="utf-8",
            )
            openssl.chmod(0o755)
            cert = tmp_path / "cert.pem"
            if create_cert:
                cert.write_text("PUBLIC CERTIFICATE FIXTURE\n", encoding="utf-8")

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["NODE_RTMPS_CERT_FILE"] = str(cert)
            env["NODE_RTMPS_KEY_FILE"] = "/secret/AUDIT_DUMMY_PRIVATE_KEY"
            if extra_env:
                env.update(extra_env)
            command = [str(CHECKER), *(args or [])]
            return subprocess.run(
                command,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

    def test_valid_certificate_is_machine_readable_and_redacted(self) -> None:
        result = self._run(extra_env={"IRLIGHT_RTMPS_CERT_MIN_VALID_SECONDS": "1209600"})
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["code"], "RTMPS_CERT_VALID")
        self.assertEqual(payload["min_valid_seconds"], 1209600)
        self.assertEqual(payload["not_after"], "Oct  1 12:00:00 2026 GMT")
        self.assertNotIn("AUDIT_DUMMY_PRIVATE_KEY", result.stdout + result.stderr)
        self.assertNotIn("cert.pem", result.stdout + result.stderr)

    def test_certificate_inside_threshold_is_warning(self) -> None:
        result = self._run(checkend_exit=1, args=["", "86400"])
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "WARNING")
        self.assertEqual(payload["code"], "RTMPS_CERT_EXPIRING")
        self.assertEqual(payload["min_valid_seconds"], 86400)

    def test_invalid_certificate_fails_closed(self) -> None:
        result = self._run(enddate_exit=1)
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["code"], "RTMPS_CERT_INVALID")

    def test_missing_certificate_fails_closed_without_printing_path(self) -> None:
        result = self._run(create_cert=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["code"], "RTMPS_CERT_UNAVAILABLE")
        self.assertNotIn("cert.pem", result.stdout + result.stderr)

    def test_non_numeric_threshold_is_configuration_error(self) -> None:
        result = self._run(extra_env={"IRLIGHT_RTMPS_CERT_MIN_VALID_SECONDS": "7days"})
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["code"], "RTMPS_CERT_THRESHOLD_INVALID")
        self.assertEqual(payload["min_valid_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
