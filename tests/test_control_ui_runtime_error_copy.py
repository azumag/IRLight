from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "apps" / "control-api" / "static" / "index.html").read_text(
    encoding="utf-8"
)
CONTINUITY = (ROOT / "apps" / "continuity" / "continuity.py").read_text(
    encoding="utf-8"
)
CONTROL_STATE = (ROOT / "apps" / "continuity" / "control_state.py").read_text(
    encoding="utf-8"
)


class ControlUiRuntimeErrorCopyContractTest(unittest.TestCase):
    def test_reason_codes_are_mapped_to_user_facing_copy(self) -> None:
        self.assertIn("const RUNTIME_ERROR_COPY = {", INDEX)
        for code in (
            "INPUT_START_FAILED",
            "INPUT_PIPELINE_ERROR",
            "PASSTHROUGH_ATTACH_FAILED",
            "OUTPUT_PIPELINE_ERROR",
            "OUTPUT_PIPELINE_EOS",
            "CONTROL_STATE_UNAVAILABLE",
            "CONTROL_STATE_INVALID",
        ):
            with self.subTest(code=code):
                self.assertIn(f"  {code}: {{", INDEX)

    def test_every_mapped_code_pairs_a_summary_with_a_required_action(self) -> None:
        block = INDEX.split("const RUNTIME_ERROR_COPY = {", 1)[1].split(
            "const RUNTIME_ERROR_UNKNOWN", 1
        )[0]
        declared = re.findall(r"^\s{2}([A-Z_]+): \{", block, re.MULTILINE)
        entries = re.findall(
            r"^\s{2}([A-Z_]+):\s*\{\s*summary:\s*'([^']+)',\s*action:\s*'([^']+)',\s*\},?$",
            block,
            re.MULTILINE,
        )
        # An entry that fails to parse (e.g. an emptied action string) must not
        # silently drop out of this check.
        self.assertEqual(
            sorted(code for code, _, _ in entries),
            sorted(declared),
            "a mapped code is missing a non-empty summary/action pair",
        )
        self.assertTrue(entries, "no mapped runtime reason codes found")
        for code, summary, action in entries:
            with self.subTest(code=code):
                self.assertTrue(summary.strip(), f"{code} has an empty summary")
                self.assertTrue(
                    action.strip(),
                    f"{code} must always state the action the user can take",
                )

    def test_unknown_reason_code_fails_closed_to_generic_copy(self) -> None:
        self.assertIn("const RUNTIME_ERROR_UNKNOWN = {", INDEX)
        self.assertIn(
            "Object.prototype.hasOwnProperty.call(RUNTIME_ERROR_COPY, prefix)",
            INDEX,
        )

        # Execute the production mapping function, not a copied approximation.
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the runtime mapping regression")
        start = INDEX.index("const RUNTIME_ERROR_COPY = {")
        end = INDEX.index("\nfunction announceStatus", start)
        production_mapping = INDEX[start:end]
        reasons = [
            "INPUT_PIPELINE_ERROR:gst-stream-error:6",
            "UNKNOWN_RUNTIME_REASON:internal-detail",
            "constructor:internal-detail",
            "toString:internal-detail",
            "__proto__:internal-detail",
        ]
        script = production_mapping + "\nconst reasons = " + json.dumps(reasons) + ";\n"
        script += "process.stdout.write(JSON.stringify(reasons.map(runtimeErrorCopy)));"
        result = subprocess.run(
            [node, "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        copies = json.loads(result.stdout)
        self.assertEqual(
            copies[0],
            {
                "summary": "入力の受信が停止しました",
                "action": "配信アプリが切断されていないか確認してください。復帰するまで待機画面を表示します。",
            },
        )
        unknown = {
            "summary": "配信を継続できません",
            "action": "しばらく待ってから再確認してください。続く場合は配信を停止して作り直してください。",
        }
        self.assertEqual(copies[1:], [unknown] * 4)

    def test_internal_reason_code_is_never_rendered_verbatim(self) -> None:
        # Only the mapped summary/action text may reach the DOM. The raw code
        # (including any ':' suffix carrying internal detail) must not.
        self.assertIn("const prefix = reason.split(':', 1)[0];", INDEX)
        self.assertIn("summary.textContent = copy.summary;", INDEX)
        self.assertNotIn("textContent = reason", INDEX)
        self.assertNotIn("textContent = r.last_error", INDEX)
        self.assertNotIn("textContent = prefix", INDEX)

    def test_absent_or_non_string_reason_renders_no_fault(self) -> None:
        self.assertIn(
            "if (typeof reason !== 'string' || reason === '') return null;", INDEX
        )

    def test_fault_row_is_fail_closed_when_runtime_is_unreachable(self) -> None:
        # A cached fault must not be presented as a current fault while the
        # runtime is stale or unavailable.
        self.assertIn(
            "renderRuntimeError(runtimeTrusted ? runtimeErrorCopy(r.last_error) : null);",
            INDEX,
        )
        self.assertIn(
            "renderRuntimeError(null);",
            INDEX,
        )

    def test_fault_row_is_inside_the_named_status_region(self) -> None:
        self.assertIn(
            '<span class="label">異常</span><span id="runtimeError" class="value" aria-describedby="runtimeErrorAction">—</span>',
            INDEX,
        )
        self.assertIn(
            '<p id="runtimeErrorAction" class="grid-note small" hidden></p>', INDEX
        )

    def test_unchanged_fault_is_not_rewritten_every_poll(self) -> None:
        self.assertIn(
            "if (action.textContent !== copy.action) action.textContent = copy.action;",
            INDEX,
        )

    def test_runtime_emitted_reason_codes_are_all_mapped(self) -> None:
        emitted = set(
            re.findall(r'"(?:INPUT|OUTPUT|PASSTHROUGH)[A-Z_]*:', CONTINUITY)
        )
        emitted = {code.strip('":') for code in emitted}
        emitted |= set(re.findall(r'"(CONTROL_STATE_[A-Z]+)"', CONTROL_STATE))
        self.assertTrue(emitted, "runtime emits no discoverable reason codes")
        mapped = set(
            re.findall(r"^\s{2}([A-Z_]+): \{", INDEX.split(
                "const RUNTIME_ERROR_COPY = {", 1
            )[1].split("const RUNTIME_ERROR_UNKNOWN", 1)[0], re.MULTILINE)
        )
        unmapped = emitted - mapped
        self.assertEqual(
            unmapped,
            set(),
            "runtime reason codes with no user-facing copy: " + ", ".join(sorted(unmapped)),
        )


    def test_fault_row_clears_summary_and_action_when_runtime_is_unreachable(self) -> None:
        # The clearing itself (not only the call site) must be present: a
        # mutant that deletes the body of the null branch would otherwise
        # keep the last fault on screen while the runtime is stale/unavailable.
        self.assertIn("summary.textContent = '—';", INDEX)
        self.assertIn("action.hidden = true;", INDEX)
        self.assertIn("action.textContent = '';", INDEX)

if __name__ == "__main__":
    unittest.main()
