#!/usr/bin/env python3
"""Render a deterministic, read-only Issue #13 Node-capacity load plan.

The planner intentionally does not execute traffic, choose acceptance thresholds,
select a safety margin, or mutate production capacity. Operators provide an
explicit media/profile label and may extend the Issue #13 baseline concurrency
ladder before handing the resulting manifest to a load harness.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Iterable

SCHEMA_VERSION = 1
BASELINE_SESSION_COUNTS = (1, 2, 4, 8)
SCENARIOS = (
    ("normal-input", "Normal input across all planned Sessions."),
    ("all-holding", "All planned Sessions are simultaneously in HOLDING."),
    ("reconnect-storm", "Planned Sessions reconnect concurrently."),
    ("asset-prefetch", "Asset pre-fetch activity is concentrated during the load level."),
    ("api-dashboard", "API/dashboard clients are active concurrently with the media load."),
)


class PlanError(ValueError):
    """Raised when a requested load plan is not safe to render."""


def normalize_profile_label(value: str) -> str:
    label = value.strip()
    if not label or "\x00" in label or "\n" in label or "\r" in label:
        raise PlanError("profile label must be one non-empty line")
    return label


def normalize_session_counts(extra_counts: Iterable[int]) -> tuple[int, ...]:
    counts = set(BASELINE_SESSION_COUNTS)
    for count in extra_counts:
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise PlanError("session counts must be positive integers")
        counts.add(count)
    return tuple(sorted(counts))


def build_plan(profile_label: str, extra_session_counts: Iterable[int] = ()) -> dict[str, object]:
    label = normalize_profile_label(profile_label)
    counts = normalize_session_counts(extra_session_counts)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_issue": 13,
        "profile_label": label,
        "session_counts": list(counts),
        "scenarios": [
            {
                "id": scenario_id,
                "description": description,
                "session_counts": list(counts),
            }
            for scenario_id, description in SCENARIOS
        ],
        "operator_inputs_required": [
            "acceptance thresholds used to classify each measured trial",
            "safety margin used when deriving max_sessions",
            "exact media/profile composition represented by profile_label",
        ],
        "execution": "plan-only",
    }


def _positive_int(raw: str) -> int:
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Render the Issue #13 Node-capacity scenario matrix without executing a load test."
        )
    )
    parser.add_argument(
        "--profile-label",
        required=True,
        help=(
            "Operator-defined media/profile composition for this run, for example "
            "'720p30 3Mbps' or an explicit 720p30/1080p30 mix."
        ),
    )
    parser.add_argument(
        "--session-count",
        action="append",
        type=_positive_int,
        default=[],
        metavar="N",
        help=(
            "Add a concurrency level beyond the Issue #13 baseline 1/2/4/8 ladder. "
            "May be repeated; duplicates are removed."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = build_plan(args.profile_label, args.session_count)
    except PlanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    json.dump(plan, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
