"""Read-only Session authority / provider inventory reconciliation.

This tool is intended for recovery drills after Control Plane writers and the
external reaper have been quiesced. It never creates, repairs, or deletes
Session state or provider resources. It compares the provider identifiers and
ownership metadata already recorded on both sides and reports discrepancies for
operator review.

Real provider access is opt-in through ``--provider-mode conoha``. The command
only calls the provider's managed-resource listing API; it has no mutation
subcommand.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable


# Source checkouts execute this file from apps/control-api while provider/ lives
# at the repository root. The runtime image already exposes /app on PYTHONPATH.
try:
    _LOCAL_REPO = Path(__file__).resolve().parents[2]
except IndexError:
    _LOCAL_REPO = None
if _LOCAL_REPO is not None and (_LOCAL_REPO / "provider").is_dir():
    if str(_LOCAL_REPO) not in sys.path:
        sys.path.insert(0, str(_LOCAL_REPO))

from provider.conoha import ManagedResource  # noqa: E402
from provider.fake_provider import (  # noqa: E402
    FakeProvider,
    FakeProviderStateError,
    FileFakeProvider,
)
from provider.provider_client import ConohaClient, ConohaConfig  # noqa: E402
from session_store import SessionStateError, SessionStore  # noqa: E402
from state_readiness import (  # noqa: E402
    StateReadinessError,
    _assert_regular_entry_unchanged,
    _assert_state_root_unchanged,
    _open_regular_readonly,
    _open_state_root,
    _read_json_authority_fd,
)


_RESOURCE_FIELDS = {
    "volume": "provider_volume_id",
    "server": "provider_server_id",
}


def _validate_session_payload(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sessions = value.get("sessions")
    if not isinstance(sessions, dict):
        raise StateReadinessError("Session authority has invalid structure")
    leases = value.get("orphan_cleanup_leases", {})
    if not isinstance(leases, dict):
        raise StateReadinessError("Session authority has invalid cleanup leases")
    try:
        SessionStore._validate_sessions(sessions)
        SessionStore._validate_cleanup_leases(leases)
    except SessionStateError as exc:
        raise StateReadinessError("Session authority failed validation") from exc
    return sessions


def load_session_snapshot(state_dir: Path) -> dict[str, dict[str, Any]]:
    """Read one validated Session snapshot without creating locks or markers."""
    root_fd = _open_state_root(state_dir)
    marker_fd: int | None = None
    authority_fd: int | None = None
    try:
        marker_fd = _open_regular_readonly(root_fd, ".sessions.json.initialized")
        authority_fd = _open_regular_readonly(root_fd, "sessions.json")
        value = _read_json_authority_fd(authority_fd)
        sessions = _validate_session_payload(value)
        _assert_regular_entry_unchanged(root_fd, "sessions.json", authority_fd)
        _assert_regular_entry_unchanged(
            root_fd, ".sessions.json.initialized", marker_fd
        )
        _assert_state_root_unchanged(state_dir, root_fd)
        # Return detached records so all pinned descriptors can be closed before
        # any provider API call. The runbook requires writers/reaper quiesced;
        # this command deliberately does not acquire or create their lock files.
        return {session_id: dict(record) for session_id, record in sessions.items()}
    finally:
        if authority_fd is not None:
            os.close(authority_fd)
        if marker_fd is not None:
            os.close(marker_fd)
        os.close(root_fd)


def load_fake_provider_snapshot(path: Path) -> FakeProvider:
    """Read a file-backed fake inventory once without following replacements."""
    root_fd = _open_state_root(path.parent)
    inventory_fd: int | None = None
    try:
        inventory_fd = _open_regular_readonly(root_fd, path.name)
        value = _read_json_authority_fd(inventory_fd)
        try:
            volumes, servers, links = FileFakeProvider._decode_state(value)
        except FakeProviderStateError as exc:
            raise StateReadinessError(
                "fake provider inventory failed validation"
            ) from exc
        _assert_regular_entry_unchanged(root_fd, path.name, inventory_fd)
        _assert_state_root_unchanged(path.parent, root_fd)

        provider = FakeProvider()
        provider.volumes = volumes
        provider.servers = servers
        provider.server_volume_links = links
        return provider
    finally:
        if inventory_fd is not None:
            os.close(inventory_fd)
        os.close(root_fd)


def _finding(
    reason_code: str,
    *,
    session_id: str | None = None,
    resource_kind: str | None = None,
    provider_id: str | None = None,
) -> dict[str, str]:
    finding = {"reason_code": reason_code}
    if session_id is not None:
        finding["session_id"] = session_id
    if resource_kind is not None:
        finding["resource_kind"] = resource_kind
    if provider_id is not None:
        finding["provider_id"] = provider_id
    return finding


def reconcile_provider_resources(
    sessions: dict[str, dict[str, Any]],
    resources: Iterable[ManagedResource],
) -> dict[str, Any]:
    """Compare state references with one read-only managed-resource snapshot.

    No lifecycle policy is inferred here. A terminal Session may legitimately
    be under cleanup and a provisioning Session may legitimately have only a
    volume. The report only checks facts that should agree when both sides
    contain them: resource IDs, Session ownership, and user ownership.
    """
    resource_list = list(resources)
    findings: list[dict[str, str]] = []
    by_key: dict[tuple[str, str], ManagedResource] = {}

    for resource in resource_list:
        if resource.kind not in _RESOURCE_FIELDS:
            findings.append(
                _finding(
                    "PROVIDER_RESOURCE_KIND_UNKNOWN",
                    session_id=resource.session_id,
                    resource_kind=resource.kind,
                    provider_id=resource.provider_id,
                )
            )
            continue
        if not isinstance(resource.provider_id, str) or not resource.provider_id:
            findings.append(
                _finding(
                    "PROVIDER_RESOURCE_ID_INVALID",
                    session_id=resource.session_id,
                    resource_kind=resource.kind,
                )
            )
            continue
        key = (resource.kind, resource.provider_id)
        if key in by_key:
            findings.append(
                _finding(
                    "PROVIDER_RESOURCE_DUPLICATE",
                    session_id=resource.session_id,
                    resource_kind=resource.kind,
                    provider_id=resource.provider_id,
                )
            )
            continue
        by_key[key] = resource

    for session_id, session in sessions.items():
        user_id = session.get("user_id")
        for kind, field in _RESOURCE_FIELDS.items():
            provider_id = session.get(field)
            if provider_id is None:
                continue
            if not isinstance(provider_id, str) or not provider_id:
                findings.append(
                    _finding(
                        "STATE_PROVIDER_REFERENCE_INVALID",
                        session_id=session_id,
                        resource_kind=kind,
                    )
                )
                continue
            resource = by_key.get((kind, provider_id))
            if resource is None:
                findings.append(
                    _finding(
                        "STATE_PROVIDER_RESOURCE_MISSING",
                        session_id=session_id,
                        resource_kind=kind,
                        provider_id=provider_id,
                    )
                )
                continue
            if resource.session_id != session_id:
                findings.append(
                    _finding(
                        "PROVIDER_SESSION_OWNERSHIP_MISMATCH",
                        session_id=session_id,
                        resource_kind=kind,
                        provider_id=provider_id,
                    )
                )
            if resource.user_id != user_id:
                findings.append(
                    _finding(
                        "PROVIDER_USER_OWNERSHIP_MISMATCH",
                        session_id=session_id,
                        resource_kind=kind,
                        provider_id=provider_id,
                    )
                )

    for resource in resource_list:
        if resource.kind not in _RESOURCE_FIELDS:
            continue
        if not isinstance(resource.provider_id, str) or not resource.provider_id:
            continue
        if not isinstance(resource.session_id, str) or not resource.session_id:
            findings.append(
                _finding(
                    "PROVIDER_SESSION_METADATA_MISSING",
                    resource_kind=resource.kind,
                    provider_id=resource.provider_id,
                )
            )
            continue
        session = sessions.get(resource.session_id)
        if session is None:
            findings.append(
                _finding(
                    "PROVIDER_RESOURCE_SESSION_MISSING",
                    session_id=resource.session_id,
                    resource_kind=resource.kind,
                    provider_id=resource.provider_id,
                )
            )
            continue
        expected = session.get(_RESOURCE_FIELDS[resource.kind])
        if expected != resource.provider_id:
            findings.append(
                _finding(
                    "PROVIDER_RESOURCE_NOT_REFERENCED_BY_SESSION",
                    session_id=resource.session_id,
                    resource_kind=resource.kind,
                    provider_id=resource.provider_id,
                )
            )
        if not isinstance(resource.user_id, str) or not resource.user_id:
            findings.append(
                _finding(
                    "PROVIDER_USER_METADATA_MISSING",
                    session_id=resource.session_id,
                    resource_kind=resource.kind,
                    provider_id=resource.provider_id,
                )
            )
        elif resource.user_id != session.get("user_id"):
            # Avoid adding the same mismatch twice when the state reference did
            # point at this exact resource.
            duplicate = any(
                item.get("reason_code") == "PROVIDER_USER_OWNERSHIP_MISMATCH"
                and item.get("session_id") == resource.session_id
                and item.get("resource_kind") == resource.kind
                and item.get("provider_id") == resource.provider_id
                for item in findings
            )
            if not duplicate:
                findings.append(
                    _finding(
                        "PROVIDER_USER_OWNERSHIP_MISMATCH",
                        session_id=resource.session_id,
                        resource_kind=resource.kind,
                        provider_id=resource.provider_id,
                    )
                )

    findings.sort(
        key=lambda item: (
            item.get("session_id", ""),
            item.get("resource_kind", ""),
            item.get("provider_id", ""),
            item["reason_code"],
        )
    )
    return {
        "status": "MATCH" if not findings else "REVIEW_REQUIRED",
        "counts": {
            "sessions": len(sessions),
            "managed_resources": len(resource_list),
            "findings": len(findings),
        },
        "findings": findings,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="provider_state_reconcile_cli")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.getenv("STATE_DIR", "/state")),
        help="quiesced Control Plane state directory",
    )
    parser.add_argument(
        "--provider-mode",
        choices=("fake", "conoha"),
        required=True,
        help="explicit provider inventory source; conoha performs read-only API calls",
    )
    parser.add_argument(
        "--fake-state-file",
        type=Path,
        default=None,
        help="FileFakeProvider state path (required with --provider-mode fake)",
    )
    return parser.parse_args(argv)


def _provider_from_args(args: argparse.Namespace):
    if args.provider_mode == "fake":
        if args.fake_state_file is None:
            raise ValueError("fake provider mode requires an explicit state file")
        try:
            return load_fake_provider_snapshot(args.fake_state_file)
        except StateReadinessError as exc:
            raise ValueError("fake provider inventory is unavailable") from exc
    return ConohaClient(ConohaConfig.from_env())


def _safe_unavailable(reason_code: str) -> dict[str, Any]:
    return {
        "status": "UNAVAILABLE",
        "reason_code": reason_code,
        "counts": {"sessions": 0, "managed_resources": 0, "findings": 0},
        "findings": [],
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        sessions = load_session_snapshot(args.state_dir)
    except (StateReadinessError, OSError):
        print(json.dumps(_safe_unavailable("SESSION_AUTHORITY_UNAVAILABLE"), sort_keys=True))
        return 2

    try:
        provider = _provider_from_args(args)
        resources = provider.list_managed_resources()
    except Exception:
        # Provider exceptions may contain request endpoints or other operator
        # context. Keep stdout/stderr suitable for sharing by returning only a
        # fixed reason code.
        print(json.dumps(_safe_unavailable("PROVIDER_INVENTORY_UNAVAILABLE"), sort_keys=True))
        return 2

    report = reconcile_provider_resources(sessions, resources)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "MATCH" else 3


if __name__ == "__main__":
    raise SystemExit(main())
