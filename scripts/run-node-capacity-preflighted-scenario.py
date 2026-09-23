#!/usr/bin/env python3
"""Run a Node-capacity scenario only after the local host preflight succeeds.

This is a thin safety wrapper around ``run-node-capacity-scenario.py``. It keeps
the existing scenario/evidence schemas unchanged while making it easy for an
operator to fail before any load harness is started when the local host does not
meet the read-only prerequisite check.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Sequence


class PreflightedScenarioError(RuntimeError):
    """Raised when the prerequisite gate cannot be evaluated safely."""


def _load_script(filename: str, module_name: str) -> ModuleType:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PreflightedScenarioError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise PreflightedScenarioError(f"cannot load {filename}") from exc
    return module


def _validate_snapshot(snapshot: object) -> None:
    if not isinstance(snapshot, dict):
        raise PreflightedScenarioError("local host preflight returned an invalid snapshot")
    if snapshot.get("schema_version") != 1:
        raise PreflightedScenarioError("local host preflight returned an unsupported schema")
    if snapshot.get("kind") != "irlight-node-capacity-host-preflight":
        raise PreflightedScenarioError("local host preflight returned an unexpected kind")
    if snapshot.get("ready") is not True:
        raise PreflightedScenarioError("local host preflight did not report ready")


def run_preflighted(
    argv: Sequence[str] | None = None,
    *,
    preflight_module: ModuleType | object | None = None,
    runner_module: ModuleType | object | None = None,
) -> int:
    """Run the existing scenario CLI only after a successful local preflight."""

    preflight = preflight_module or _load_script(
        "check-node-capacity-host-preflight.py",
        "irlight_node_capacity_host_preflight_for_runner",
    )
    try:
        snapshot = preflight.collect_snapshot()
    except preflight.HostPreflightError as exc:
        # Keep the wrapper boundary intentionally generic. The standalone
        # preflight command remains available when an operator needs its bounded
        # local diagnostic, but a failed gate must not start the load harness.
        raise PreflightedScenarioError("local host prerequisite check failed") from exc
    _validate_snapshot(snapshot)

    runner = runner_module or _load_script(
        "run-node-capacity-scenario.py",
        "irlight_node_capacity_scenario_runner_after_preflight",
    )
    return int(runner.main(list(argv) if argv is not None else None))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run_preflighted(argv)
    except PreflightedScenarioError as exc:
        print(f"node capacity scenario preflight failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
