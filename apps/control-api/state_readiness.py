"""Read-only readiness checks for Control Plane authority files.

The normal stores intentionally create lock files, initialization markers, and
first-run authority. Readiness must never use those code paths: a diagnostic
request must not turn a missing or mis-mounted state volume into apparently
valid empty state.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Callable

from auth_store import _validate_sessions, _validate_users
from catalog_store import CatalogValidationError, _validate_destination_server_url
from control_store import _validate_control
from node_internal import _validate_tokens, validate_node_authority
from state_safety import initialization_marker


class StateReadinessError(RuntimeError):
    """Raised when required authority cannot be inspected safely."""


Validator = Callable[[dict[str, Any]], object]


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constants are not allowed")


def _open_regular_readonly(path: Path) -> int:
    """Open one existing regular file without following a raced symlink."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise StateReadinessError("required state entry is unavailable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise StateReadinessError("required state entry is not a regular file")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise StateReadinessError("required state entry cannot be opened") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise StateReadinessError("required state entry is not a regular file")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise StateReadinessError("required state entry changed during inspection")
        return fd
    except Exception:
        os.close(fd)
        raise


def _require_regular_marker(authority_path: Path) -> None:
    fd = _open_regular_readonly(initialization_marker(authority_path))
    os.close(fd)


def _read_json_authority(path: Path) -> dict[str, Any]:
    fd = _open_regular_readonly(path)
    try:
        try:
            handle = os.fdopen(fd, "r", encoding="utf-8")
        except OSError as exc:
            raise StateReadinessError("required state cannot be read") from exc
        fd = -1  # ``handle`` owns the descriptor from this point forward.
        with handle:
            try:
                value = json.load(handle, parse_constant=_reject_json_constant)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError) as exc:
                raise StateReadinessError("required state contains invalid JSON") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(value, dict):
        raise StateReadinessError("required state has invalid structure")
    return value


def _inspect_authority(path: Path, validator: Validator) -> None:
    # Startup writers arm the marker before publishing authority. Requiring
    # both entries catches deleted/mis-mounted state without creating either.
    _require_regular_marker(path)
    value = _read_json_authority(path)
    try:
        validator(value)
    except StateReadinessError:
        raise
    except Exception as exc:
        raise StateReadinessError("required state failed validation") from exc


def _validate_catalog(value: dict[str, Any]) -> dict[str, Any]:
    destinations = value.get("destinations")
    assets = value.get("assets")
    if not isinstance(destinations, dict) or not isinstance(assets, dict):
        raise StateReadinessError("catalog has invalid structure")
    for section in (destinations, assets):
        if any(
            not isinstance(key, str) or not isinstance(item, dict)
            for key, item in section.items()
        ):
            raise StateReadinessError("catalog has an invalid record")
    for item in destinations.values():
        server_url = item.get("server_url")
        if not isinstance(server_url, str):
            continue
        try:
            _validate_destination_server_url(server_url)
        except CatalogValidationError as exc:
            raise StateReadinessError("catalog contains an unsafe destination") from exc
    return value


def _inspect_optional_legacy_token_fuse(node_state_dir: Path) -> None:
    """Validate the rollback token ledger when it exists, without creating it."""
    path = node_state_dir / "bootstrap_tokens.json"
    marker = initialization_marker(path)
    try:
        path_stat = path.lstat()
        path_exists = True
    except FileNotFoundError:
        path_exists = False
        path_stat = None
    except OSError as exc:
        raise StateReadinessError("legacy token fuse cannot be inspected") from exc

    try:
        marker.lstat()
        marker_exists = True
    except FileNotFoundError:
        marker_exists = False
    except OSError as exc:
        raise StateReadinessError("legacy token fuse marker cannot be inspected") from exc

    if not path_exists:
        if marker_exists:
            # A consumed-token write-ahead fuse disappeared. Treating that as
            # an empty ledger can make rollback reuse a credential.
            raise StateReadinessError("legacy token fuse disappeared after initialization")
        return
    if path_stat is None or not stat.S_ISREG(path_stat.st_mode):
        raise StateReadinessError("legacy token fuse is not a regular file")
    if marker_exists:
        _require_regular_marker(path)
    value = _read_json_authority(path)
    try:
        _validate_tokens(value)
    except Exception as exc:
        raise StateReadinessError("legacy token fuse failed validation") from exc


def check_state_readiness(
    *,
    state_dir: Path,
    node_state_dir: Path | None = None,
) -> None:
    """Validate startup-mandatory authority without locks or writes.

    The five canonical files below are initialized synchronously by ``app.py``.
    Lazy stores (for example Session/entitlement/secret authorities) retain
    their own fail-closed request paths and can be added to readiness only when
    their deployment lifecycle is made mandatory.
    """
    control_path = state_dir / "control.json"
    catalog_path = state_dir / "catalog.json"
    users_path = state_dir / "users.json"
    auth_sessions_path = state_dir / "auth_sessions.json"
    effective_node_state_dir = node_state_dir or state_dir
    nodes_path = effective_node_state_dir / "nodes.json"

    _inspect_authority(control_path, _validate_control)
    _inspect_authority(catalog_path, _validate_catalog)
    _inspect_authority(users_path, _validate_users)
    _inspect_authority(auth_sessions_path, _validate_sessions)
    _inspect_authority(nodes_path, validate_node_authority)
    _inspect_optional_legacy_token_fuse(effective_node_state_dir)
