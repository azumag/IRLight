import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "dependency-audit.yml"
DOC = ROOT / "docs" / "dependency-security-audit.md"


class DependencySbomWorkflowTests(unittest.TestCase):
    def test_workflow_generates_and_validates_cyclonedx_sbom(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("- name: Generate runtime dependency SBOM", text)
        self.assertIn("--format cyclonedx-json", text)
        self.assertIn("--output /tmp/runtime-dependency-sbom.cdx.json", text)
        self.assertIn("- name: Validate runtime dependency SBOM", text)
        self.assertIn('sbom.get("bomFormat") != "CycloneDX"', text)
        self.assertIn(
            'required_components = {"fastapi", "starlette", "uvicorn", "cryptography"}',
            text,
        )

    def test_workflow_retains_sbom_as_bounded_artifact(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("- name: Retain runtime dependency SBOM", text)
        self.assertIn("name: runtime-dependency-sbom", text)
        self.assertIn("path: /tmp/runtime-dependency-sbom.cdx.json", text)
        self.assertIn("if-no-files-found: error", text)
        self.assertIn("retention-days: 7", text)

    def test_documentation_states_sbom_scope_and_secret_boundary(self) -> None:
        text = DOC.read_text(encoding="utf-8")

        self.assertIn("CycloneDX JSON SBOM", text)
        self.assertIn("`runtime-dependency-sbom` artifact", text)
        self.assertIn("secret、production credential、stream key、配信内容は収集しない", text)
        self.assertIn("container image の digest pinning", text)


if __name__ == "__main__":
    unittest.main()
