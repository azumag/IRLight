from __future__ import annotations

import ast
import fnmatch
import shlex
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTINUITY_DIR = ROOT / "apps" / "continuity"
DOCKERFILE = CONTINUITY_DIR / "Dockerfile"


def _runtime_local_dependencies(entrypoint: str) -> set[str]:
    pending = [CONTINUITY_DIR / entrypoint]
    required: set[str] = set()

    while pending:
        path = pending.pop()
        if path.name in required:
            continue
        required.add(path.name)

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported_modules.add(node.module.split(".", 1)[0])

        for module in imported_modules:
            candidate = CONTINUITY_DIR / f"{module}.py"
            if candidate.is_file() and candidate.name not in required:
                pending.append(candidate)

    return required


def _dockerfile_python_sources() -> set[str]:
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    copied: set[str] = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("COPY ") or "--from=" in line:
            continue
        tokens = shlex.split(line)
        if len(tokens) < 3:
            continue
        for source in tokens[1:-1]:
            if source in {".", "./"}:
                return {path.name for path in CONTINUITY_DIR.glob("*.py")}
            if any(char in source for char in "*?["):
                copied.update(
                    path.name
                    for path in CONTINUITY_DIR.glob("*.py")
                    if fnmatch.fnmatch(path.name, Path(source).name)
                )
            elif source.endswith(".py"):
                copied.add(Path(source).name)

    return copied


class ContinuityDockerfilePackagingTests(unittest.TestCase):
    def test_runner_runtime_local_modules_are_copied_into_image(self) -> None:
        required = _runtime_local_dependencies("runner.py")
        copied = _dockerfile_python_sources()

        missing = sorted(required - copied)
        self.assertEqual(
            missing,
            [],
            "Continuity Dockerfile omits runtime local module(s): " + ", ".join(missing),
        )


if __name__ == "__main__":
    unittest.main()
