from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_API_DIR = ROOT / "apps" / "control-api"
DOCKERFILE = CONTROL_API_DIR / "Dockerfile"
RUNTIME_ENTRYPOINTS = (
    "app.py",
    "reaper_cli.py",
    "state_inspect_cli.py",
)


class ControlApiImagePackagingTest(unittest.TestCase):
    def test_runtime_local_import_closure_is_packaged(self) -> None:
        """Keep first-party runtime imports present in the Control API image.

        The Dockerfile currently enumerates Python sources explicitly. This test
        follows static imports from each runtime entrypoint so a newly introduced
        first-party module cannot pass source-level unit tests while being omitted
        from the image. Dynamic imports are intentionally outside this contract.
        """

        required = _runtime_local_import_closure()
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        packaged = _packaged_python_sources(dockerfile)

        missing = sorted(required - packaged)
        self.assertEqual(
            missing,
            [],
            "Control API Docker image is missing runtime Python module(s): "
            + ", ".join(missing),
        )


def _runtime_local_import_closure() -> set[str]:
    pending = list(RUNTIME_ENTRYPOINTS)
    visited: set[str] = set()

    while pending:
        filename = pending.pop()
        if filename in visited:
            continue

        path = CONTROL_API_DIR / filename
        if not path.is_file():
            raise AssertionError(f"runtime entrypoint/module does not exist: {filename}")

        visited.add(filename)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module_name in _imported_top_level_modules(tree):
            candidate = f"{module_name}.py"
            if (CONTROL_API_DIR / candidate).is_file() and candidate not in visited:
                pending.append(candidate)

    return visited


def _imported_top_level_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".", 1)[0])
    return modules


def _packaged_python_sources(dockerfile: str) -> set[str]:
    # A future switch to copying the whole control-api directory satisfies the
    # contract without maintaining an explicit filename manifest.
    if re.search(r"(?m)^COPY\s+apps/control-api/?\s+", dockerfile):
        return {path.name for path in CONTROL_API_DIR.glob("*.py")}

    return set(re.findall(r"apps/control-api/([A-Za-z0-9_]+\.py)\b", dockerfile))


if __name__ == "__main__":
    unittest.main()
