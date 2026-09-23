#!/usr/bin/env python3
"""Atomically persist a validated Node-capacity review bundle.

The renderer intentionally writes to stdout for composability. Shell redirection,
however, truncates an existing artifact before validation runs. This wrapper
renders and validates the complete evidence closure first, then replaces the
requested repository-relative output in one atomic rename so a failed refresh
cannot destroy the last known-good review bundle.

It never edits scheduler inventory or Node configuration, executes load, contacts
a provider, or chooses capacity policy.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_RENDERER_PATH = Path(__file__).with_name("render-node-capacity-review-bundle.py")


class CapacityReviewBundleWriteError(ValueError):
    """Raised when a review bundle cannot be persisted safely."""


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CapacityReviewBundleWriteError("capacity review bundle renderer could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise CapacityReviewBundleWriteError(
            "capacity review bundle renderer could not be loaded"
        ) from exc
    return module


BUNDLE_RENDERER = _load_module(
    BUNDLE_RENDERER_PATH,
    "irlight_node_capacity_review_bundle_atomic_renderer",
)


def _canonical_relative_path(path: Path) -> str:
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise CapacityReviewBundleWriteError(
            "review bundle output must be a repository-relative file path"
        )
    return path.as_posix()


def _pinned_paths(bundle: dict[str, Any]) -> set[str]:
    try:
        paths = {
            str(bundle["proposal_path"]),
            str(bundle["coverage_manifest"]),
            str(bundle["load_plan"]["path"]),
        }
        for report in bundle["reports"]:
            paths.add(str(report["path"]))
            if "trials_path" in report:
                paths.add(str(report["trials_path"]))
            if "run_manifest_path" in report:
                paths.add(str(report["run_manifest_path"]))

        host_provenance = bundle.get("host_provenance")
        if host_provenance is not None:
            paths.add(str(host_provenance["path"]))
            for scenario in host_provenance["scenarios"]:
                paths.add(str(scenario["host_preflight"]["path"]))
    except (KeyError, TypeError) as exc:
        raise CapacityReviewBundleWriteError("rendered review bundle is invalid") from exc
    return paths


def _resolve_output(output: Path, *, bundle: dict[str, Any], repo_root: Path) -> Path:
    relative = _canonical_relative_path(output)
    if relative in _pinned_paths(bundle):
        raise CapacityReviewBundleWriteError(
            "review bundle output must not replace pinned capacity evidence"
        )

    try:
        root = repo_root.resolve(strict=True)
    except OSError as exc:
        raise CapacityReviewBundleWriteError("repository root could not be resolved") from exc

    current = root
    for part in output.parts[:-1]:
        current = current / part
        try:
            snapshot = os.lstat(current)
        except OSError as exc:
            raise CapacityReviewBundleWriteError(
                "review bundle output parent must already exist"
            ) from exc
        if stat.S_ISLNK(snapshot.st_mode) or not stat.S_ISDIR(snapshot.st_mode):
            raise CapacityReviewBundleWriteError(
                "review bundle output parent must be a real directory"
            )

    target = root / output
    try:
        existing = os.lstat(target)
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise CapacityReviewBundleWriteError("review bundle output could not be inspected") from exc
    if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)):
        raise CapacityReviewBundleWriteError(
            "review bundle output must be a regular file when it already exists"
        )
    return target


def write_bundle_atomically(
    bundle: dict[str, Any],
    output: Path,
    *,
    repo_root: Path = ROOT,
) -> Path:
    """Write one already-rendered bundle without truncating a known-good artifact."""

    target = _resolve_output(output, bundle=bundle, repo_root=repo_root)
    payload = json.dumps(
        bundle,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"

    fd = -1
    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        temp_name = None
    except (OSError, TypeError, ValueError) as exc:
        raise CapacityReviewBundleWriteError(
            "review bundle could not be persisted atomically"
        ) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return target


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "proposal",
        type=Path,
        help="repository-relative persisted Node-capacity max_sessions proposal",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="repository-relative review bundle JSON to replace atomically",
    )
    parser.add_argument(
        "--host-provenance",
        type=Path,
        help="optional repository-relative validated host-provenance sidecar; emits schema v3",
    )
    parser.add_argument(
        "--node-profile",
        required=True,
        help="exact Node profile of the proposed deployment",
    )
    parser.add_argument(
        "--software-revision",
        required=True,
        help="exact lowercase 40-character Git commit SHA of the proposed deployment",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        bundle = BUNDLE_RENDERER.render_bundle(
            args.proposal,
            expected_node_profile=args.node_profile,
            expected_software_revision=args.software_revision,
            host_provenance_path=args.host_provenance,
        )
        write_bundle_atomically(bundle, args.output)
    except (
        BUNDLE_RENDERER.CapacityReviewBundleRenderError,
        CapacityReviewBundleWriteError,
    ) as exc:
        print(f"node capacity review bundle write failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
