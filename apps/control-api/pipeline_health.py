"""Translate Node media-stack health into a guarded Session failure signal.

A single failed Docker health observation must not destroy a Session: the media
containers use restart policies and can legitimately disappear for one
heartbeat while recovering.  The Control Plane therefore persists an
unhealthy-since timestamp in Node state and only latches ``PIPELINE_CRASHED``
after a configurable grace period.

The Node can bypass the grace by explicitly reporting ``status=FAILED`` when it
knows the media stack is unrecoverable.
"""

from __future__ import annotations

import os
import time
from typing import Any

from session_store import ACTIVE_STATES, SessionStore


DEFAULT_MEDIA_HEALTH_FAILURE_GRACE_SECONDS = 30.0
_FATAL_MEDIA_HEALTH = {"stopped", "failed", "crashed"}


def media_health_failure_grace_seconds() -> float:
    raw = os.getenv(
        "NODE_MEDIA_HEALTH_FAILURE_GRACE_SECONDS",
        str(DEFAULT_MEDIA_HEALTH_FAILURE_GRACE_SECONDS),
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MEDIA_HEALTH_FAILURE_GRACE_SECONDS
    return max(0.0, value)


def apply_pipeline_health(
    store: SessionStore,
    *,
    node: dict[str, Any],
    node_id: str,
    session_id: str,
    node_status: str,
    media_health: str,
    observed_at: float | None = None,
    grace_seconds: float | None = None,
) -> bool:
    """Apply one media-health observation.

    Returns ``True`` only when this call newly latches a fatal pipeline failure.
    The ``node`` dictionary is mutated so its unhealthy timer and desired state
    are persisted together with the heartbeat record.
    """

    current = time.time() if observed_at is None else float(observed_at)
    normalized_health = str(media_health).strip().lower()
    normalized_status = str(node_status).strip().upper()

    if normalized_health == "running":
        node.pop("media_unhealthy_since", None)
        node.pop("media_failure_reason", None)
        return False

    explicitly_failed = normalized_status == "FAILED"
    if not explicitly_failed and normalized_health not in _FATAL_MEDIA_HEALTH:
        # UNKNOWN or a future non-fatal health value is not proof of recovery,
        # but it also must not start/latch a fatal timer by itself.
        return False

    raw_since = node.get("media_unhealthy_since")
    try:
        unhealthy_since = float(raw_since) if raw_since is not None else None
    except (TypeError, ValueError):
        unhealthy_since = None
    if unhealthy_since is None or current < unhealthy_since:
        unhealthy_since = current
        node["media_unhealthy_since"] = current
    node["media_failure_reason"] = "PIPELINE_CRASHED"

    configured_grace = (
        media_health_failure_grace_seconds()
        if grace_seconds is None
        else max(0.0, float(grace_seconds))
    )
    if not explicitly_failed and current - unhealthy_since < configured_grace:
        return False

    session = store.get(session_id)
    if session is None:
        raise KeyError(session_id)
    if session.get("node_id") not in {None, node_id}:
        raise RuntimeError("node is not assigned to session")

    from_state = str(session.get("status", ""))
    if from_state not in ACTIVE_STATES:
        # STOPPING / FAILED_CLEANUP and terminal states win races with a late
        # health observation.  In particular, user stop must never become a
        # pipeline failure.
        return False

    store.transition(
        session_id,
        "FAILED_CLEANUP",
        allow_from=ACTIVE_STATES,
        cleanup_pending=True,
        failure_reason="media pipeline remained unavailable",
        failure_reason_code="PIPELINE_CRASHED",
    )
    node["desired_state"] = "STOPPED"
    node["pipeline_failure_latched_at"] = current
    store.append_event(
        session_id,
        event_type="session.failure_detected",
        reason_code="PIPELINE_CRASHED",
        payload={
            "node_id": node_id,
            "from_state": from_state,
            "to_state": "FAILED_CLEANUP",
            "cleanup_pending": True,
            "media_health": str(media_health)[:100],
            "unhealthy_since": unhealthy_since,
        },
        origin="node-agent",
        occurred_at=current,
    )
    return True
