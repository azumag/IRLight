"""Read-only administrative inspection for Control Plane state authority."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from state_readiness import inspect_state_readiness


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="state_inspect_cli")
    parser.add_argument(
        "--state-dir",
        default=os.getenv("STATE_DIR", "/state"),
        help="Control Plane state directory (default: STATE_DIR or /state)",
    )
    parser.add_argument(
        "--node-state-dir",
        default=None,
        help="Node authority directory (default: NODE_STATE_DIR or STATE_DIR)",
    )
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    node_state_dir = Path(
        args.node_state_dir
        or os.getenv("NODE_STATE_DIR", str(state_dir))
    )
    checks = inspect_state_readiness(
        state_dir=state_dir,
        node_state_dir=node_state_dir,
    )
    ready = all(check["status"] == "OK" for check in checks)
    print(
        json.dumps(
            {"status": "READY" if ready else "UNAVAILABLE", "checks": checks},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
