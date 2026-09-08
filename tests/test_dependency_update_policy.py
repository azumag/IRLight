from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPENDABOT = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
POLICY = (ROOT / "docs" / "dependency-update-policy.md").read_text(encoding="utf-8")
VULNERABILITY_RUNBOOK = (
    ROOT / "docs" / "dependency-vulnerability-response.md"
).read_text(encoding="utf-8")
VULNERABILITY_TEMPLATE = (
    ROOT / ".github" / "ISSUE_TEMPLATE" / "dependency-vulnerability.md"
).read_text(encoding="utf-8")


class DependencyUpdatePolicyTest(unittest.TestCase):
    def test_dependabot_covers_runtime_dependencies_actions_and_images(self) -> None:
        self.assertEqual(DEPENDABOT.count('package-ecosystem: "pip"'), 1)
        self.assertEqual(DEPENDABOT.count('package-ecosystem: "github-actions"'), 1)
        self.assertEqual(DEPENDABOT.count('package-ecosystem: "docker"'), 2)
        for directory in (
            'directory: "/apps/control-api"',
            'directory: "/apps/node-agent"',
            'directory: "/"',
        ):
            with self.subTest(directory=directory):
                self.assertIn(directory, DEPENDABOT)

    def test_dependabot_updates_are_weekly_and_bounded(self) -> None:
        self.assertEqual(DEPENDABOT.count('interval: "weekly"'), 4)
        self.assertEqual(DEPENDABOT.count('timezone: "Asia/Tokyo"'), 4)
        limits = [
            int(line.split(":", 1)[1].strip())
            for line in DEPENDABOT.splitlines()
            if line.strip().startswith("open-pull-requests-limit:")
        ]
        self.assertEqual(len(limits), 4)
        self.assertTrue(all(1 <= limit <= 5 for limit in limits))

    def test_policy_keeps_automated_updates_behind_existing_merge_gates(self) -> None:
        for gate in (
            "Dependency audit",
            "CI",
            "Disconnect recovery E2E",
            "RTMPS ingest recovery E2E",
            "SRT ingest recovery E2E",
        ):
            with self.subTest(gate=gate):
                self.assertIn(gate, POLICY)
        self.assertIn("自動マージはしない", POLICY)

    def test_policy_tracks_sbom_as_active_artifact_and_links_response_runbook(self) -> None:
        self.assertIn("CycloneDX JSON SBOM", POLICY)
        self.assertIn("docs/dependency-vulnerability-response.md", POLICY)
        self.assertIn("container image digest pinning と release artifact signing", POLICY)

    def test_vulnerability_runbook_is_reproducible_and_keeps_merge_gates(self) -> None:
        for expected in (
            "runtime-dependency-audit",
            "runtime-dependency-sbom",
            "python -m pip check",
            "python -m pip_audit",
            "--strict",
            "--requirement apps/control-api/requirements.txt",
            "Dependency audit",
            "Disconnect recovery E2E",
            "RTMPS ingest recovery E2E",
            "SRT ingest recovery E2E",
            "--ignore-vuln",
            "有効期限",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, VULNERABILITY_RUNBOOK)

    def test_vulnerability_issue_template_records_actionable_exception_context(self) -> None:
        for expected in (
            "about: Dependency audit / advisory",
            'labels: ""',
            'assignees: ""',
            "Advisory ID",
            "Current version",
            "Fix version",
            "Compensating control",
            "有効期限",
            "docs/dependency-vulnerability-response.md",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, VULNERABILITY_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
