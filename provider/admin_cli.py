"""Admin CLI for the ConoHa provider spike.

Commands:
  create <session-id>   ensure a boot volume and a server for the session
  list                  show all IRLight-managed provider resources
  delete <session-id>   delete server then boot volume for the session
  inventory             show managed resource inventory (same as list)
  cleanup-proof         print the manual verification checklist

Use `--fake` to run against an in-memory provider; without it the real
ConoHa API is used via CONOHA_* environment variables.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

from provider.conoha import (
    SessionMetadata,
    format_timestamp,
    is_safe_session_id,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="admin_cli")
    parser.add_argument("--fake", action="store_true", help="use FakeProvider")
    parser.add_argument("--environment", default="dev", choices=["dev", "beta", "prod"])
    parser.add_argument("--user-id", default="deadbeef", help="opaque user id")
    parser.add_argument("--session-id", default=None, help="default session id")
    parser.add_argument("--size-gb", type=int, default=20)
    parser.add_argument("--delete-after-hours", type=float, default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create")
    subparsers.add_parser("list")
    subparsers.add_parser("delete")
    subparsers.add_parser("inventory")
    subparsers.add_parser("cleanup-proof")
    return parser.parse_args(argv)


def _session_id_from_args(args: argparse.Namespace) -> str:
    session_id = args.session_id or str(uuid.uuid4())
    if not is_safe_session_id(session_id):
        raise SystemExit(f"invalid session id: {session_id!r}")
    return session_id


def _get_provider(fake: bool):
    if fake:
        from provider.fake_provider import FileFakeProvider

        path = Path(os.getenv("FAKE_PROVIDER_STATE", "/tmp/irlight-fake-provider.json"))
        return FileFakeProvider(path)
    from provider.provider_client import ConohaClient, ConohaConfig

    return ConohaClient(ConohaConfig.from_env())


def _create(provider, args: argparse.Namespace) -> None:
    session_id = _session_id_from_args(args)
    delete_after = (
        time.time() + args.delete_after_hours * 3600
        if args.delete_after_hours is not None
        else None
    )
    metadata = SessionMetadata(
        session_id=session_id,
        user_id=args.user_id,
        environment=args.environment,
        delete_after=delete_after,
    )
    image_ref = os.getenv("CONOHA_IMAGE_REF", "ubuntu-24.04")
    flavor_ref = os.getenv("CONOHA_FLAVOR_REF", "g2")

    print(f"ensure boot volume for session {session_id}...")
    volumes = provider.list_volumes()
    existing_volume = next(
        (v for v in volumes if v.tags.get("irlight-session-id") == session_id and v.is_managed),
        None,
    )
    if existing_volume is not None:
        print(f"  existing volume: {existing_volume.volume_id}")
        volume = existing_volume
    else:
        volume = provider.create_volume(
            name=f"irlight-{session_id}-boot",
            size_gb=args.size_gb,
            metadata=metadata.as_tags(),
        )
        print(f"  created volume:  {volume.volume_id}")

    print(f"ensure server for session {session_id}...")
    servers = provider.list_servers()
    existing_server = next(
        (s for s in servers if s.tags.get("irlight-session-id") == session_id and s.is_managed),
        None,
    )
    if existing_server is not None:
        print(f"  existing server: {existing_server.server_id}")
        server = existing_server
    else:
        server = provider.create_server(
            name=f"irlight-{session_id}-node",
            image_ref=image_ref,
            flavor_ref=flavor_ref,
            volume_id=volume.volume_id,
            metadata=metadata.as_tags(),
        )
        print(f"  created server:  {server.server_id}")

    print(
        json_summary(
            {
                "session_id": session_id,
                "volume_id": volume.volume_id,
                "server_id": server.server_id,
                "status": server.status,
                "public_ipv4": server.public_ipv4,
                "metadata": metadata.as_tags(),
            }
        )
    )


def _list(provider) -> None:
    for resource in sorted(
        provider.list_managed_resources(), key=lambda r: (r.kind, r.provider_id)
    ):
        print(
            f"{resource.kind:<8} {resource.provider_id} session={resource.session_id or '-'} "
            f"user={resource.user_id or '-'} created_at={iso_or_dash(resource.created_at)}"
        )


def _delete(provider, session_id: str) -> None:
    resources = provider.list_managed_resources()
    servers = [r for r in resources if r.kind == "server" and r.session_id == session_id]
    volumes = [r for r in resources if r.kind == "volume" and r.session_id == session_id]

    for server in servers:
        provider.delete_server(server.provider_id)
        print(f"deleted server  {server.provider_id} (session {session_id})")
    for volume in volumes:
        provider.delete_volume(volume.provider_id)
        print(f"deleted volume  {volume.provider_id} (session {session_id})")

    remaining = [
        r for r in provider.list_managed_resources() if r.session_id == session_id
    ]
    if remaining:
        print(f"ERROR: {len(remaining)} resource(s) remain", file=sys.stderr)
        sys.exit(2)
    print(f"cleanup complete for session {session_id}")


def _cleanup_proof() -> None:
    print(
        """Manual cleanup proof checklist
============================
1. Run: CONOHA_* envs set; admin_cli create <session-id>
2. Run: admin_cli list        -> both volume and server appear
3. Run: admin_cli delete <session-id>
4. Run: admin_cli list        -> no IRLight-managed resources remain
5. Confirm credentials never appeared in shell output or logs
6. Confirm delete-after tag is set when --delete-after-hours was given
"""
    )


def iso_or_dash(value: float | None) -> str:
    if value is None:
        return "-"
    return format_timestamp(value)


def json_summary(payload: dict[str, object]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    provider = _get_provider(args.fake)
    if args.command == "create":
        _create(provider, args)
    elif args.command in ("list", "inventory"):
        _list(provider)
    elif args.command == "delete":
        _delete(provider, _session_id_from_args(args))
    elif args.command == "cleanup-proof":
        _cleanup_proof()
    else:
        raise SystemExit(f"unknown command: {args.command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
