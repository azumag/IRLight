import fnmatch
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    "disconnect-recovery.yml",
    "rtmps-ingest-recovery.yml",
    "srt-ingest-recovery.yml",
)


class DependencyRecoveryWorkflowTests(unittest.TestCase):
    def test_dependency_updates_trigger_all_recovery_gates(self):
        # These are the dependency surfaces covered by the update policy,
        # including npm manifests/locks and Actions updates outside this workflow.
        paths = (
            "apps/control-api/requirements.txt",
            "apps/control-api/Dockerfile",
            "apps/node-agent/Dockerfile",
            "apps/continuity/Dockerfile",
            "apps/egress-gateway/Dockerfile",
            "apps/control-plane/package.json",
            "apps/control-plane/package-lock.json",
            ".github/workflows/ci.yml",
            ".github/workflows/dependency-audit.yml",
            ".github/workflows/control-plane.yml",
        )
        for workflow in WORKFLOWS:
            source = (ROOT / ".github/workflows" / workflow).read_text()
            # Limit matching to the pull_request paths, not schedule or steps.
            trigger = source.split("  pull_request:\n", 1)[1]
            trigger = re.split(r"\n  [a-z_]+:", trigger, maxsplit=1)[0]
            patterns = re.findall(r'^      - "([^"]+)"$', trigger, re.MULTILINE)
            self.assertTrue(patterns, workflow)
            for path in paths:
                with self.subTest(workflow=workflow, path=path):
                    self.assertTrue(
                        any(fnmatch.fnmatchcase(path, p) for p in patterns),
                        f"{workflow} does not run for {path}",
                    )

    def test_docs_only_changes_do_not_trigger_recovery(self):
        for workflow in WORKFLOWS:
            source = (ROOT / ".github/workflows" / workflow).read_text()
            trigger = source.split("  pull_request:\n", 1)[1]
            trigger = re.split(r"\n  [a-z_]+:", trigger, maxsplit=1)[0]
            patterns = re.findall(r'^      - "([^"]+)"$', trigger, re.MULTILINE)
            for path in ("README.md", "docs/dependency-update-policy.md"):
                with self.subTest(workflow=workflow, path=path):
                    self.assertFalse(any(fnmatch.fnmatchcase(path, p) for p in patterns))


if __name__ == "__main__":
    unittest.main()
