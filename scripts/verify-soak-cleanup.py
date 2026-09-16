#!/usr/bin/env python3
"""Verify that a disposable IRLight soak Compose project left no Docker resources.

This verifier is intentionally read-only. It only accepts project names generated
for soak runs and inspects exact Docker Compose project labels on containers,
networks, volumes, and images. It never removes resources itself.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Callable


class CleanupVerificationError(RuntimeError):
    """Raised when cleanup cannot be verified reliably."""


PROJECT_RE = re.compile(r"^irlight-poc-soak-[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def validate_project_name(project: str) -> str:
    if not PROJECT_RE.fullmatch(project):
        raise CleanupVerificationError(
            "project must be a disposable IRLight soak project named irlight-poc-soak-*"
        )
    return project


def run_checked(argv: list[str], *, timeout: float = 15.0) -> str:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CleanupVerificationError(
            f"command failed to execute: {argv[0]}: {exc}"
        ) from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        detail = f": {stderr}" if stderr else ""
        raise CleanupVerificationError(
            f"command exited {completed.returncode}: {argv[0]}{detail}"
        )
    return completed.stdout


def _lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


def inspect_project_resources(
    project: str,
    runner: Callable[[list[str]], str] = run_checked,
) -> dict[str, list[str]]:
    project = validate_project_name(project)
    label = f"label=com.docker.compose.project={project}"
    commands = {
        "containers": ["docker", "ps", "-aq", "--filter", label],
        "networks": ["docker", "network", "ls", "-q", "--filter", label],
        "volumes": ["docker", "volume", "ls", "-q", "--filter", label],
        "images": ["docker", "image", "ls", "-q", "--filter", label],
    }
    return {kind: _lines(runner(argv)) for kind, argv in commands.items()}


def verify_cleanup(
    project: str,
    runner: Callable[[list[str]], str] = run_checked,
) -> dict[str, object]:
    resources = inspect_project_resources(project, runner)
    leftovers = {kind: ids for kind, ids in resources.items() if ids}
    return {
        "project": project,
        "verified": not leftovers,
        "leftovers": leftovers,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = verify_cleanup(args.project)
    except CleanupVerificationError as exc:
        print(f"soak cleanup verification failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
