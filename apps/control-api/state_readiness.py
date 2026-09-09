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
from state_safety import load_json_authority


class StateReadinessError(RuntimeError):
    """Raised when required authority cannot be inspected safely."""


Validator = Callable[[dict[str, Any]], object]


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constants are not allowed")


def _open_state_root(path: Path) -> int:
    """Pin one configured state root without following the root itself."""
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise StateReadinessError("state directory cannot be inspected safely")
    try:
        before = path.lstat()
    except OSError as exc:
        raise StateReadinessError("state directory is unavailable") from exc
    if not stat.S_ISDIR(before.st_mode):
        raise StateReadinessError("state directory is not a directory")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except (OSError, NotImplementedError) as exc:
        raise StateReadinessError("state directory cannot be opened safely") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise StateReadinessError("state directory is not a directory")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise StateReadinessError("state directory changed while opening")
        return fd
    except Exception:
        os.close(fd)
        raise


def _assert_state_root_unchanged(path: Path, root_fd: int) -> None:
    """Fail closed if the configured pathname stopped naming the pinned root."""
    try:
        after = path.lstat()
        opened = os.fstat(root_fd)
    except OSError as exc:
        raise StateReadinessError("state directory changed during inspection") from exc
    if not stat.S_ISDIR(after.st_mode):
        raise StateReadinessError("state directory changed during inspection")
    if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
        raise StateReadinessError("state directory changed during inspection")


def _open_regular_readonly(root_fd: int, name: str) -> int:
    """Open one direct child of a pinned state root without following symlinks."""
    try:
        before = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except (OSError, NotImplementedError) as exc:
        raise StateReadinessError("required state entry is unavailable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise StateReadinessError("required state entry is not a regular file")

    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(name, flags, dir_fd=root_fd)
    except (OSError, NotImplementedError) as exc:
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


def _assert_regular_entry_unchanged(root_fd: int, name: str, fd: int) -> None:
    """Fail closed if a pinned authority path was replaced during inspection."""
    try:
        current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        opened = os.fstat(fd)
    except (OSError, NotImplementedError) as exc:
        raise StateReadinessError("required state entry changed during inspection") from exc
    if not stat.S_ISREG(current.st_mode):
        raise StateReadinessError("required state entry changed during inspection")
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise StateReadinessError("required state entry changed during inspection")


def _marker_name(authority_name: str) -> str:
    return f".{authority_name}.initialized"


def _read_json_authority_fd(fd: int) -> dict[str, Any]:
    """Read strict JSON from an already pinned regular-file descriptor."""
    try:
        handle = os.fdopen(os.dup(fd), "r", encoding="utf-8")
    except OSError as exc:
        raise StateReadinessError("required state cannot be read") from exc
    with handle:
        try:
            value = load_json_authority(handle, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError) as exc:
            raise StateReadinessError("required state contains invalid JSON") from exc
    if not isinstance(value, dict):
        raise StateReadinessError("required state has invalid structure")
    return value


def _inspect_authority(root_fd: int, authority_name: str, validator: Validator) -> None:
    # Startup writers arm the marker before publishing authority. Pinning both
    # names until validation completes prevents a concurrent atomic replace
    # from making readiness approve an already-stale inode snapshot.
    marker_name = _marker_name(authority_name)
    marker_fd = _open_regular_readonly(root_fd, marker_name)
    authority_fd: int | None = None
    try:
        authority_fd = _open_regular_readonly(root_fd, authority_name)
        value = _read_json_authority_fd(authority_fd)
        try:
            validator(value)
        except StateReadinessError:
            raise
        except Exception as exc:
            raise StateReadinessError("required state failed validation") from exc
        _assert_regular_entry_unchanged(root_fd, authority_name, authority_fd)
        _assert_regular_entry_unchanged(root_fd, marker_name, marker_fd)
    finally:
        if authority_fd is not None:
            os.close(authority_fd)
        os.close(marker_fd)


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


def _optional_entry_stat(
    root_fd: int, name: str, *, marker: bool = False
) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except (OSError, NotImplementedError) as exc:
        detail = (
            "legacy token fuse marker cannot be inspected"
            if marker
            else "legacy token fuse cannot be inspected"
        )
        raise StateReadinessError(detail) from exc


def _inspect_optional_legacy_token_fuse(node_root_fd: int) -> None:
    """Validate the rollback token ledger when it exists, without creating it."""
    authority_name = "bootstrap_tokens.json"
    marker_name = _marker_name(authority_name)
    path_stat = _optional_entry_stat(node_root_fd, authority_name)
    marker_stat = _optional_entry_stat(node_root_fd, marker_name, marker=True)

    if path_stat is None:
        if marker_stat is not None:
            # A consumed-token write-ahead fuse disappeared. Treating that as
            # an empty ledger can make rollback reuse a credential.
            raise StateReadinessError("legacy token fuse disappeared after initialization")
        return
    if not stat.S_ISREG(path_stat.st_mode):
        raise StateReadinessError("legacy token fuse is not a regular file")
    if marker_stat is not None and not stat.S_ISREG(marker_stat.st_mode):
        raise StateReadinessError("legacy token fuse marker is not a regular file")

    authority_fd = _open_regular_readonly(node_root_fd, authority_name)
    marker_fd: int | None = None
    try:
        if marker_stat is not None:
            marker_fd = _open_regular_readonly(node_root_fd, marker_name)
        value = _read_json_authority_fd(authority_fd)
        try:
            _validate_tokens(value)
        except Exception as exc:
            raise StateReadinessError("legacy token fuse failed validation") from exc
        _assert_regular_entry_unchanged(node_root_fd, authority_name, authority_fd)
        if marker_fd is not None:
            _assert_regular_entry_unchanged(node_root_fd, marker_name, marker_fd)
        elif _optional_entry_stat(node_root_fd, marker_name, marker=True) is not None:
            # Marker absence is part of the compatibility snapshot for legacy
            # ledgers. If a writer initializes the fuse while validation is in
            # flight, do not report readiness from the stale pre-marker view.
            raise StateReadinessError("required state entry changed during inspection")
    finally:
        if marker_fd is not None:
            os.close(marker_fd)
        os.close(authority_fd)


def _authority_specs(
    *, state_dir: Path, node_state_dir: Path | None = None
) -> tuple[tuple[str, Path, Validator], ...]:
    """Return the canonical path-based spec used by restore comparison tooling."""
    effective_node_state_dir = node_state_dir or state_dir
    return (
        ("control", state_dir / "control.json", _validate_control),
        ("catalog", state_dir / "catalog.json", _validate_catalog),
        ("users", state_dir / "users.json", _validate_users),
        ("auth_sessions", state_dir / "auth_sessions.json", _validate_sessions),
        ("nodes", effective_node_state_dir / "nodes.json", validate_node_authority),
    )


def _authority_fd_specs(
    *, state_root_fd: int, node_root_fd: int
) -> tuple[tuple[str, int, str, Validator], ...]:
    return (
        ("control", state_root_fd, "control.json", _validate_control),
        ("catalog", state_root_fd, "catalog.json", _validate_catalog),
        ("users", state_root_fd, "users.json", _validate_users),
        ("auth_sessions", state_root_fd, "auth_sessions.json", _validate_sessions),
        ("nodes", node_root_fd, "nodes.json", validate_node_authority),
    )


def _replace_checks_with_root_error(
    checks: list[dict[str, str | None]],
    *,
    authorities: set[str],
    error: StateReadinessError,
) -> None:
    for check in checks:
        if check["authority"] in authorities:
            check["status"] = "UNAVAILABLE"
            check["reason"] = str(error)


def inspect_state_readiness(
    *,
    state_dir: Path,
    node_state_dir: Path | None = None,
) -> list[dict[str, str | None]]:
    """Inspect every startup authority and return safe operator diagnostics.

    The returned records contain only a stable authority label, status, and a
    normalized validation reason. They intentionally omit file paths, raw JSON,
    parser exceptions, credentials, and validator exception details.
    """
    effective_node_state_dir = node_state_dir or state_dir
    state_root_fd: int | None = None
    node_root_fd: int | None = None
    state_root_error: StateReadinessError | None = None
    node_root_error: StateReadinessError | None = None
    try:
        try:
            state_root_fd = _open_state_root(state_dir)
        except StateReadinessError as exc:
            state_root_error = exc
        try:
            node_root_fd = _open_state_root(effective_node_state_dir)
        except StateReadinessError as exc:
            node_root_error = exc

        checks: list[dict[str, str | None]] = []
        specs: tuple[
            tuple[str, int | None, StateReadinessError | None, str, Validator], ...
        ] = (
            ("control", state_root_fd, state_root_error, "control.json", _validate_control),
            ("catalog", state_root_fd, state_root_error, "catalog.json", _validate_catalog),
            ("users", state_root_fd, state_root_error, "users.json", _validate_users),
            (
                "auth_sessions",
                state_root_fd,
                state_root_error,
                "auth_sessions.json",
                _validate_sessions,
            ),
            ("nodes", node_root_fd, node_root_error, "nodes.json", validate_node_authority),
        )
        for authority, root_fd, root_error, authority_name, validator in specs:
            if root_error is not None or root_fd is None:
                error = root_error or StateReadinessError("state directory is unavailable")
                checks.append(
                    {"authority": authority, "status": "UNAVAILABLE", "reason": str(error)}
                )
                continue
            try:
                _inspect_authority(root_fd, authority_name, validator)
            except StateReadinessError as exc:
                checks.append(
                    {"authority": authority, "status": "UNAVAILABLE", "reason": str(exc)}
                )
            else:
                checks.append({"authority": authority, "status": "OK", "reason": None})

        if node_root_error is not None or node_root_fd is None:
            error = node_root_error or StateReadinessError("state directory is unavailable")
            checks.append(
                {
                    "authority": "legacy_bootstrap_tokens",
                    "status": "UNAVAILABLE",
                    "reason": str(error),
                }
            )
        else:
            try:
                _inspect_optional_legacy_token_fuse(node_root_fd)
            except StateReadinessError as exc:
                checks.append(
                    {
                        "authority": "legacy_bootstrap_tokens",
                        "status": "UNAVAILABLE",
                        "reason": str(exc),
                    }
                )
            else:
                checks.append(
                    {"authority": "legacy_bootstrap_tokens", "status": "OK", "reason": None}
                )

        if state_root_fd is not None:
            try:
                _assert_state_root_unchanged(state_dir, state_root_fd)
            except StateReadinessError as exc:
                _replace_checks_with_root_error(
                    checks,
                    authorities={"control", "catalog", "users", "auth_sessions"},
                    error=exc,
                )
        if node_root_fd is not None:
            try:
                _assert_state_root_unchanged(effective_node_state_dir, node_root_fd)
            except StateReadinessError as exc:
                _replace_checks_with_root_error(
                    checks,
                    authorities={"nodes", "legacy_bootstrap_tokens"},
                    error=exc,
                )
        return checks
    finally:
        if state_root_fd is not None:
            os.close(state_root_fd)
        if node_root_fd is not None:
            os.close(node_root_fd)


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
    effective_node_state_dir = node_state_dir or state_dir
    state_root_fd = _open_state_root(state_dir)
    try:
        node_root_fd = _open_state_root(effective_node_state_dir)
        try:
            for _authority, root_fd, authority_name, validator in _authority_fd_specs(
                state_root_fd=state_root_fd,
                node_root_fd=node_root_fd,
            ):
                _inspect_authority(root_fd, authority_name, validator)
            _inspect_optional_legacy_token_fuse(node_root_fd)
            _assert_state_root_unchanged(state_dir, state_root_fd)
            _assert_state_root_unchanged(effective_node_state_dir, node_root_fd)
        finally:
            os.close(node_root_fd)
    finally:
        os.close(state_root_fd)
