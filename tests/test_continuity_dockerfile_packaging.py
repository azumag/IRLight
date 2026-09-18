from __future__ import annotations

import ast
import fnmatch
import shlex
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTINUITY_DIR = ROOT / "apps" / "continuity"
DOCKERFILE = CONTINUITY_DIR / "Dockerfile"
IMAGE_WORKDIR = "/app"
IMAGE_ENTRYPOINTS = (
    "runner.py",
    "make_default_standby.py",
)


def _runtime_local_dependencies(entrypoint: str) -> set[str]:
    pending = [CONTINUITY_DIR / entrypoint]
    required: set[str] = set()

    while pending:
        path = pending.pop()
        if path.name in required:
            continue
        if not path.is_file():
            raise AssertionError(f"Continuity image entrypoint/module does not exist: {path.name}")
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


def _copy_destination_is_workdir(destination: str) -> bool:
    return destination in {".", "./", IMAGE_WORKDIR, f"{IMAGE_WORKDIR}/"}


def _dockerfile_python_sources(dockerfile: str | None = None) -> set[str]:
    text = (
        DOCKERFILE.read_text(encoding="utf-8") if dockerfile is None else dockerfile
    ).replace("\\\n", " ")
    copied: set[str] = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("COPY "):
            continue
        tokens = shlex.split(line)
        if len(tokens) < 3 or any(token.startswith("--from=") for token in tokens[1:]):
            continue

        destination = tokens[-1]
        if not _copy_destination_is_workdir(destination):
            continue

        sources = [token for token in tokens[1:-1] if not token.startswith("--")]
        for source in sources:
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
    def test_image_entrypoint_local_modules_are_copied_into_workdir(self) -> None:
        required: set[str] = set()
        for entrypoint in IMAGE_ENTRYPOINTS:
            required.update(_runtime_local_dependencies(entrypoint))
        copied = _dockerfile_python_sources()

        missing = sorted(required - copied)
        self.assertEqual(
            missing,
            [],
            "Continuity Dockerfile omits image local module(s) from /app: "
            + ", ".join(missing),
        )

    def test_copy_to_other_destination_does_not_satisfy_contract(self) -> None:
        copied = _dockerfile_python_sources(
            "COPY runner.py standby_integrity.py make_default_standby.py /tmp/continuity/\n"
        )
        self.assertEqual(copied, set())

    def test_directory_copy_to_workdir_packages_all_python_sources(self) -> None:
        copied = _dockerfile_python_sources("COPY . /app/\n")
        self.assertEqual(
            copied,
            {path.name for path in CONTINUITY_DIR.glob("*.py")},
        )


if __name__ == "__main__":
    unittest.main()
