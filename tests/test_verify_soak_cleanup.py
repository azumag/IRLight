from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_soak_cleanup", ROOT / "scripts" / "verify-soak-cleanup.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CleanupVerificationError = MODULE.CleanupVerificationError


class SoakCleanupVerificationTest(unittest.TestCase):
    def test_project_name_is_restricted_to_disposable_soak_projects(self) -> None:
        self.assertEqual(
            MODULE.validate_project_name("irlight-poc-soak-123-456"),
            "irlight-poc-soak-123-456",
        )
        for invalid in (
            "irlight-poc",
            "production",
            "irlight-poc-soak-../prod",
            "irlight-poc-soak-",
            "irlight-poc-soak-$HOME",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(CleanupVerificationError):
                    MODULE.validate_project_name(invalid)

    def test_clean_project_is_verified_with_exact_compose_label_filters(self) -> None:
        calls: list[list[str]] = []

        def runner(argv: list[str]) -> str:
            calls.append(argv)
            return ""

        result = MODULE.verify_cleanup("irlight-poc-soak-123-456", runner)
        self.assertEqual(
            result,
            {
                "project": "irlight-poc-soak-123-456",
                "verified": True,
                "leftovers": {},
            },
        )
        self.assertEqual(len(calls), 4)
        for argv in calls:
            self.assertIn(
                "label=com.docker.compose.project=irlight-poc-soak-123-456",
                argv,
            )
        self.assertEqual(calls[0][:3], ["docker", "ps", "-aq"])
        self.assertEqual(calls[1][:3], ["docker", "network", "ls"])
        self.assertEqual(calls[2][:3], ["docker", "volume", "ls"])
        self.assertEqual(calls[3][:3], ["docker", "image", "ls"])

    def test_leftovers_fail_verification_and_preserve_resource_kinds(self) -> None:
        outputs = iter(("container-a\n", "", "volume-a\nvolume-b\n", "image-a\n"))
        result = MODULE.verify_cleanup(
            "irlight-poc-soak-run1", lambda _argv: next(outputs)
        )
        self.assertFalse(result["verified"])
        self.assertEqual(
            result["leftovers"],
            {
                "containers": ["container-a"],
                "volumes": ["volume-a", "volume-b"],
                "images": ["image-a"],
            },
        )

    def test_command_failure_is_not_treated_as_clean(self) -> None:
        def runner(_argv: list[str]) -> str:
            raise CleanupVerificationError("docker unavailable")

        with self.assertRaisesRegex(CleanupVerificationError, "docker unavailable"):
            MODULE.verify_cleanup("irlight-poc-soak-run1", runner)

    def test_main_exit_codes_distinguish_clean_leftovers_and_unverifiable(self) -> None:
        original = MODULE.verify_cleanup
        try:
            MODULE.verify_cleanup = lambda project: {
                "project": project,
                "verified": True,
                "leftovers": {},
            }
            self.assertEqual(MODULE.main(["--project", "irlight-poc-soak-run1"]), 0)
            MODULE.verify_cleanup = lambda project: {
                "project": project,
                "verified": False,
                "leftovers": {"containers": ["abc"]},
            }
            self.assertEqual(MODULE.main(["--project", "irlight-poc-soak-run1"]), 1)

            def fail(_project: str):
                raise CleanupVerificationError("cannot inspect")

            MODULE.verify_cleanup = fail
            self.assertEqual(MODULE.main(["--project", "irlight-poc-soak-run1"]), 2)
        finally:
            MODULE.verify_cleanup = original


if __name__ == "__main__":
    unittest.main()
