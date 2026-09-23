#!/usr/bin/env python3
"""Classify pull-request paths for the Docker-heavy CI suite.

Only repository documentation under ``docs/`` is currently allowed to bypass
Docker integration and measured-soak execution. Any empty, malformed, or
otherwise unknown path set fails closed to the heavy suite.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

DOCS_ONLY_PREFIX = "docs/"


def requires_heavy_ci(paths: Iterable[str]) -> bool:
    """Return True unless every changed path is an ordinary docs/ path."""
    normalized: list[str] = []
    for raw_path in paths:
        path = raw_path.rstrip("\r\n")
        if not path:
            continue
        normalized.append(path)

    if not normalized:
        return True

    for path in normalized:
        if (
            not path.startswith(DOCS_ONLY_PREFIX)
            or path == DOCS_ONLY_PREFIX
            or path.startswith("/")
            or "\x00" in path
        ):
            return True
    return False


def main() -> int:
    print("true" if requires_heavy_ci(sys.stdin) else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
