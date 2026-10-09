"""Offline inventory contract; it does not certify runtime security or deployment."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = Path("docs/threat-model.md")
# Independent inventory copied from Issue #12, rather than inferred from the doc.
THREATS = {
    "T01": "出力先stream keyの漏えい",
    "T02": "入力資格情報の総当たり・共有・再利用",
    "T03": "Custom RTMP URLを用いたSSRF・内部ネットワーク探索",
    "T04": "不正なメディア入力によるprocess crash・DoS",
    "T05": "巨大画像・動画によるresource exhaustion",
    "T06": "無料枠・イベントパスの大量取得",
    "T07": "違法・権利侵害コンテンツの中継",
    "T08": "管理者権限の濫用",
    "T09": "Node侵害時のsecret流出",
    "T10": "ログ・監視・例外通知へのsecret混入",
    "T11": "依存コンテナ・FFmpeg/GStreamer等の脆弱性",
}
FIELDS = ("状態", "既存統制・根拠", "回帰テスト", "残ギャップ")
LINK = re.compile(r"\[[^\]\n]+\]\(([^\s()]+)\)")
THREAT_HEADING = re.compile(r"^### (T\d+): (.+)$", re.MULTILINE)
TEST_SYMBOL = re.compile(r"`([A-Za-z_][\w]*\.test_[\w]+)`")
SECTIONS = (
    "対象資産", "攻撃者の前提", "信頼境界", "状態の読み方",
    "11脅威と統制・回帰テスト", "残ギャップの追跡と更新契約",
)
SOURCE_LINKS = {
    "https://github.com/azumag/IRLight/issues/12",
    "https://github.com/azumag/IRLight/issues/12#issuecomment-6057892683",
    "https://github.com/azumag/IRLight/pull/645",
    "https://github.com/azumag/IRLight/issues/545",
    "https://github.com/azumag/IRLight/pull/646",
    "https://github.com/azumag/IRLight/pull/647",
}


def markdown_anchors(text: str) -> set[str]:
    """Heading slugs for this document's plain headings, including duplicates."""
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for heading in re.findall(r"^#{1,6} (.+)$", text, re.MULTILINE):
        slug = re.sub(r"[^\w\-\s]", "", heading.lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        anchors.add(f"{slug}-{count}" if count else slug)
    return anchors


def local_target(source: Path, url: str) -> Path | None:
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc:
        return None
    if not parsed.path:
        return (REPO_ROOT / source).resolve()
    return (REPO_ROOT / source.parent / unquote(parsed.path)).resolve()


def link_errors(source: Path, text: str) -> list[str]:
    errors = []
    for url in LINK.findall(text):
        parsed = urlsplit(url)
        target = local_target(source, url)
        if target is None:
            if parsed.scheme != "https" or parsed.netloc != "github.com":
                errors.append(f"unsupported external link: {url}")
            continue
        if not target.is_relative_to(REPO_ROOT) or not target.is_file():
            errors.append(f"missing local target: {url}")
        elif parsed.fragment:
            if target.suffix != ".md" or unquote(parsed.fragment) not in markdown_anchors(
                target.read_text(encoding="utf-8")
            ):
                errors.append(f"missing local anchor: {url}")
    return errors


def test_symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        f"{cls.name}.{method.name}"
        for cls in tree.body if isinstance(cls, ast.ClassDef)
        for method in cls.body
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
        and method.name.startswith("test_")
    }


def model_errors(text: str) -> list[str]:
    errors = link_errors(MODEL_PATH, text)
    for section in SECTIONS:
        if f"\n## {section}\n" not in text:
            errors.append(f"missing section: {section}")
    for url in SOURCE_LINKS - set(LINK.findall(text)):
        errors.append(f"missing source: {url}")

    headings = list(THREAT_HEADING.finditer(text))
    if [match.group(1) for match in headings] != list(THREATS):
        errors.append("threat inventory must contain T01..T11 exactly once in order")
    for index, heading in enumerate(headings):
        threat_id, title = heading.groups()
        if THREATS.get(threat_id) != title:
            errors.append(f"wrong threat title: {threat_id}")
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[heading.end():end].split("\n## ", 1)[0]
        fields = {}
        for field in FIELDS:
            matches = re.findall(rf"^- {re.escape(field)}: (.+)$", block, re.MULTILINE)
            if len(matches) != 1:
                errors.append(f"missing or duplicate field: {threat_id} {field}")
            else:
                fields[field] = matches[0]

        evidence = fields.get("既存統制・根拠", "")
        paths = [local_target(MODEL_PATH, url) for url in LINK.findall(evidence)]
        if not any(p and p.suffix == ".md" for p in paths):
            errors.append(f"missing documentation evidence: {threat_id}")
        if not any(p and p.suffix != ".md" for p in paths):
            errors.append(f"missing code/config evidence: {threat_id}")

        regressions = fields.get("回帰テスト", "")
        links = list(LINK.finditer(regressions))
        qualified_count = 0
        for link_index, link in enumerate(links):
            path = local_target(MODEL_PATH, link.group(1))
            if path is None or not path.is_relative_to(REPO_ROOT / "tests"):
                errors.append(f"regression reference must be a local test: {threat_id}")
                continue
            if not path.is_file() or path.suffix != ".py":
                continue  # Already reported by link_errors.
            next_link = links[link_index + 1].start() if link_index + 1 < len(links) else len(regressions)
            symbols = TEST_SYMBOL.findall(regressions[link.end():next_link])
            qualified_count += len(symbols)
            actual = test_symbols(path)
            if not actual:
                errors.append(f"empty regression module: {path.name}")
            for symbol in symbols:
                if symbol not in actual:
                    errors.append(f"missing regression symbol: {path.name} {symbol}")
        if not qualified_count:
            errors.append(f"missing named regression: {threat_id}")
    return errors


def index_errors(source: Path, text: str) -> list[str]:
    links = [url for url in LINK.findall(text) if local_target(source, url) == REPO_ROOT / MODEL_PATH]
    if len(links) != 1:
        return [f"index must link threat model exactly once: {source}"]
    # Validate the selected link only; unrelated index links have their own contracts.
    return link_errors(source, f"[threat model]({links[0]})")


class ThreatModelContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (REPO_ROOT / MODEL_PATH).read_text(encoding="utf-8")

    def test_model_inventory_evidence_links_and_regressions(self) -> None:
        self.assertEqual(model_errors(self.text), [])

    def test_entry_indexes_link_model(self) -> None:
        for source in (Path("README.md"), Path("docs/operations/README.md")):
            with self.subTest(source=source):
                self.assertEqual(index_errors(source, (REPO_ROOT / source).read_text(encoding="utf-8")), [])

    def test_contract_is_discovered_by_existing_ci(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("python -m unittest discover -s tests -v", workflow)

    def test_missing_duplicate_and_renamed_threats_are_detected(self) -> None:
        start = self.text.index("### T05:")
        end = self.text.index("### T06:")
        variants = (
            self.text[:start] + self.text[end:],
            self.text[:end] + self.text[start:end] + self.text[end:],
            self.text.replace("### T05:", "### T99:"),
            self.text.replace(THREATS["T05"], "incomplete threat title"),
        )
        for variant in variants:
            with self.subTest(variant=variant[start:start + 60]):
                self.assertTrue(any("threat" in error for error in model_errors(variant)))

    def test_missing_gap_or_evidence_is_detected(self) -> None:
        for field in FIELDS:
            with self.subTest(field=field):
                mutated = re.sub(rf"^- {re.escape(field)}: .+\n", "", self.text, count=1, flags=re.MULTILINE)
                self.assertIn(f"missing or duplicate field: T01 {field}", model_errors(mutated))

    def test_broken_evidence_file_and_anchor_are_detected(self) -> None:
        for old, new, reason in (
            ("../apps/control-api/destination_secret_store.py", "../apps/control-api/missing-evidence.py", "missing local target"),
            ("egress-secret-delivery.md)", "egress-secret-delivery.md#missing-anchor)", "missing local anchor"),
            ("../apps/control-api/destination_secret_store.py", "../../../../outside-evidence.py", "missing local target"),
        ):
            with self.subTest(reason=reason):
                self.assertTrue(any(reason in error for error in model_errors(self.text.replace(old, new, 1))))

    def test_stale_regression_symbol_is_detected(self) -> None:
        text = self.text.replace(
            "DestinationSecretStoreTest.test_same_ref_is_isolated_by_user",
            "DestinationSecretStoreTest.test_missing_regression",
        )
        self.assertTrue(any("missing regression symbol" in error for error in model_errors(text)))

    def test_index_removal_and_broken_anchor_are_detected(self) -> None:
        for source in (Path("README.md"), Path("docs/operations/README.md")):
            text = (REPO_ROOT / source).read_text(encoding="utf-8")
            url = next(url for url in LINK.findall(text) if local_target(source, url) == REPO_ROOT / MODEL_PATH)
            with self.subTest(source=source):
                self.assertTrue(index_errors(source, text.replace(f"]({url})", "](missing-model.md)")))
                self.assertTrue(index_errors(source, text.replace(f"]({url})", f"]({url}#missing-anchor)")))

    def test_model_local_anchor_link_is_accepted(self) -> None:
        for source, url in (
            (MODEL_PATH, "#信頼境界"),
            (Path("README.md"), "docs/threat-model.md#信頼境界"),
        ):
            with self.subTest(source=source):
                self.assertEqual(link_errors(source, f"[boundary]({url})"), [])


if __name__ == "__main__":
    unittest.main()
