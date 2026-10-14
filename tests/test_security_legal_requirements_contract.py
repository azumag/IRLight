"""Offline contract for Issue #12 security/legal draft requirements."""

from __future__ import annotations

from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class SecurityLegalRequirementsContractTest(unittest.TestCase):
    REQUIRED_DOCS = (
        "docs/legal/terms-of-service-draft.md",
        "docs/legal/privacy-policy-draft.md",
        "docs/legal/tokushoho-disclosure-draft.md",
        "docs/legal/security-contact.md",
        "docs/operations/data-retention-deletion.md",
        "docs/operations/abuse-report-takedown.md",
    )

    KEYWORDS = {
        "docs/legal/terms-of-service-draft.md": (
            "ベストエフォート",
            "禁止",
            "録画",
            "返金",
        ),
        "docs/legal/privacy-policy-draft.md": (
            "収集",
            "保持期間",
            "削除請求",
            "Cookie",
        ),
        "docs/legal/tokushoho-disclosure-draft.md": (
            "販売事業者",
            "所在地",
            "返品",
        ),
        "docs/legal/security-contact.md": (
            "連絡先",
            "脆弱性",
            "security@",
        ),
        "docs/operations/data-retention-deletion.md": (
            "映像",
            "保持",
            "削除",
        ),
        "docs/operations/abuse-report-takedown.md": (
            "通報",
            "停止",
            "異議",
        ),
    }

    def test_required_docs_exist(self) -> None:
        for relative in self.REQUIRED_DOCS:
            with self.subTest(path=relative):
                self.assertTrue((REPO_ROOT / relative).is_file())

    def test_required_keywords_present(self) -> None:
        for relative, keywords in self.KEYWORDS.items():
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            for keyword in keywords:
                with self.subTest(path=relative, keyword=keyword):
                    self.assertIn(keyword, text)

    def test_operations_index_links_new_docs(self) -> None:
        text = (REPO_ROOT / "docs/operations/README.md").read_text(encoding="utf-8")
        for relative in (
            "../legal/terms-of-service-draft.md",
            "../legal/privacy-policy-draft.md",
            "../legal/tokushoho-disclosure-draft.md",
            "../legal/security-contact.md",
            "data-retention-deletion.md",
            "abuse-report-takedown.md",
        ):
            with self.subTest(path=relative):
                self.assertIn(relative, text)

    def test_readme_mentions_legal_drafts(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/legal/", text)