"""Integrate media-control reconciliation with the existing Agent HTTP transport."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from media_control_heartbeat import (
    add_audio_observation,
    apply_audio_command,
    bootstrap_audio_command,
)
from media_control_state import MediaControlStateError


HttpJson = Callable[..., dict[str, object]]


def media_control_http_json(
    delegate: HttpJson,
    *,
    status_path: Path,
    control_path: Path,
) -> HttpJson:
    """Wrap ``agent.http_json`` without changing its public call contract.

    Media-control failures are isolated from the lifecycle heartbeat: malformed
    local status emits no ACK, and a conflicting/malformed remote command never
    mutates local authority, but STOP/deadline responses can still reach the
    Agent loop. Repeated installation with the same paths is a no-op so tests
    or embedded entrypoints cannot accidentally nest the adapter.
    """

    if (
        getattr(delegate, "__irlight_media_control_status_path__", None) == status_path
        and getattr(delegate, "__irlight_media_control_control_path__", None)
        == control_path
    ):
        return delegate

    def wrapped(
        url: str,
        *,
        method: str,
        token: str | None = None,
        payload: dict[str, object] | None = None,
        timeout: float = 15.0,
    ) -> dict[str, object]:
        heartbeat = method == "POST" and url.rstrip("/").endswith("/heartbeat")
        bootstrap = method == "POST" and url.rstrip("/").endswith("/nodes/bootstrap")
        outgoing = payload
        if heartbeat and payload is not None:
            try:
                outgoing = add_audio_observation(payload, status_path=status_path)
            except MediaControlStateError as exc:
                print(
                    f"[agent] media-control observation unavailable: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                outgoing = payload

        response = delegate(
            url,
            method=method,
            token=token,
            payload=outgoing,
            timeout=timeout,
        )

        if bootstrap:
            try:
                command = bootstrap_audio_command(response)
            except MediaControlStateError as exc:
                print(
                    f"[agent] bootstrap media-control authority rejected: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return response
            if command is not None:
                translated = dict(response)
                translated.update(
                    {
                        "audio_mode": command.get("desired_mode", "LIVE"),
                        "audio_version": command.get("version", 0),
                        "audio_command_id": command.get("command_id"),
                        "audio_idempotency_key": command.get("idempotency_key"),
                        "audio_updated_at": command.get("updated_at"),
                    }
                )
                response = translated

        if heartbeat:
            try:
                apply_audio_command(response, control_path=control_path)
            except MediaControlStateError as exc:
                print(
                    f"[agent] media-control command rejected: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        return response

    setattr(wrapped, "__irlight_media_control_status_path__", status_path)
    setattr(wrapped, "__irlight_media_control_control_path__", control_path)
    return wrapped
