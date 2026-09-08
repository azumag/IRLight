"""Read-only Media Node heartbeat inspection for operators.

The inspector intentionally reads only the canonical Node authority and emits a
small redacted summary. It never acquires the Node state lock, creates missing
state, or invokes provider/reaper mutations.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import time
from pathlib import Path
from typing import Any

from node_internal import validate_node_authority
from state_safety import initialization_marker, load_json_authority


class NodeHeartbeatInspectError(RuntimeError):
    """Raised when Node heartbeat authority cannot be inspected safely."""


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constants are not allowed")


def _open_regular_readonly(path: Path) -> int:
    try:
        before = path.lstat()
    except OSError as exc:
        raise NodeHeartbeatInspectError("required state entry is unavailable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise NodeHeartbeatInspectError("required state entry is not a regular file")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise NodeHeartbeatInspectError("required state entry cannot be opened") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise NodeHeartbeatInspectError("required state entry is not a regular file")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise NodeHeartbeatInspectError("required state entry changed during inspection")
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_node_authority(node_state_dir: Path) -> dict[str, Any]:
    path = node_state_dir / "nodes.json"

    marker_fd = _open_regular_readonly(initialization_marker(path))
    os.close(marker_fd)

    fd = _open_regular_readonly(path)
    try:
        try:
            handle = os.fdopen(fd, "r", encoding="utf-8")
        except OSError as exc:
            raise NodeHeartbeatInspectError("required state cannot be read") from exc
        fd = -1
        with handle:
            try:
                value = load_json_authority(
                    handle, parse_constant=_reject_json_constant
                )
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError) as exc:
                raise NodeHeartbeatInspectError(
                    "required state contains invalid JSON"
                ) from exc
    finally:
        if fd >= 0:
            os.close(fd)

    if not isinstance(value, dict):
        raise NodeHeartbeatInspectError("required state has invalid structure")
    try:
        validate_node_authority(value)
    except Exception as exc:
        raise NodeHeartbeatInspectError("required state failed validation") from exc
    return value


def summarize_node_heartbeats(
    authority: dict[str, Any], *, now: float, grace_seconds: float
) -> dict[str, Any]:
    """Return a redacted heartbeat summary from already validated Node authority."""
    if not math.isfinite(now) or now < 0:
        raise ValueError("now must be a finite non-negative timestamp")
    if not math.isfinite(grace_seconds) or grace_seconds <= 0:
        raise ValueError("grace_seconds must be a finite positive number")

    summaries: list[dict[str, Any]] = []
    stale_count = 0
    expected_running_count = 0

    nodes = authority["nodes"]
    for node_id in sorted(nodes):
        node = nodes[node_id]
        last_heartbeat_at = node.get("last_heartbeat_at")
        heartbeat_seen = last_heartbeat_at is not None
        baseline = float(last_heartbeat_at if heartbeat_seen else node["created_at"])
        age_seconds = max(0.0, now - baseline)

        expected_heartbeat = (
            node["desired_state"] == "RUNNING"
            and node["status"] not in {"STOPPED", "FAILED"}
        )
        stale = expected_heartbeat and age_seconds >= grace_seconds
        if expected_heartbeat:
            expected_running_count += 1
        if stale:
            stale_count += 1

        summaries.append(
            {
                "node_id": node_id,
                "session_id": node["session_id"],
                "status": node["status"],
                "desired_state": node["desired_state"],
                "expected_heartbeat": expected_heartbeat,
                "heartbeat_seen": heartbeat_seen,
                "heartbeat_age_seconds": round(age_seconds, 3)
                if heartbeat_seen
                else None,
                "registration_age_seconds": round(age_seconds, 3)
                if not heartbeat_seen
                else None,
                "stale": stale,
            }
        )

    return {
        "status": "STALE" if stale_count else "OK",
        "heartbeat_grace_seconds": grace_seconds,
        "node_count": len(summaries),
        "expected_running_count": expected_running_count,
        "stale_count": stale_count,
        "nodes": summaries,
    }


def _positive_finite(value: str) -> float:
    try:
        numeric = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(numeric) or numeric <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return numeric


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_heartbeat_inspect_cli")
    parser.add_argument(
        "--node-state-dir",
        default=os.getenv("NODE_STATE_DIR", os.getenv("STATE_DIR", "/state")),
        help="Node authority directory (default: NODE_STATE_DIR, STATE_DIR, or /state)",
    )
    parser.add_argument(
        "--heartbeat-grace-seconds",
        type=_positive_finite,
        default=os.getenv("NODE_HEARTBEAT_GRACE_SECONDS", "120"),
        help="Age at which an expected heartbeat is stale (default: env or 120)",
    )
    args = parser.parse_args(argv)

    try:
        authority = _read_node_authority(Path(args.node_state_dir))
        payload = summarize_node_heartbeats(
            authority,
            now=time.time(),
            grace_seconds=args.heartbeat_grace_seconds,
        )
    except NodeHeartbeatInspectError:
        print(
            json.dumps(
                {"status": "UNAVAILABLE", "reason": "node authority unavailable"},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 3

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 2 if payload["stale_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
