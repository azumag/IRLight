"""In-memory fake of the ConoHa provider for tests and dry-run flows.

The fake keeps the same resource lifecycle shape as the real client:
volumes must exist before servers, and deleting a server does not delete its
boot volume automatically (delete_on_termination is false by design).

``FileFakeProvider`` persists the same state to a JSON file so a multi-command
CLI run (create -> list -> delete) behaves like the real provider.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from provider.conoha import (
    ManagedResource,
    ProviderServer,
    ProviderVolume,
    managed_since,
    format_timestamp,
    parse_timestamp,
)


class FakeProviderStateError(RuntimeError):
    """Raised when file-backed fake provider state cannot be trusted."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _reject_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is not allowed: {key}")
        result[key] = value
    return result


def _require_string(value: Any, *, context: str) -> str:
    if not isinstance(value, str):
        raise FakeProviderStateError(f"invalid {context}")
    return value


def _require_string_map(value: Any, *, context: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise FakeProviderStateError(f"invalid {context}")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise FakeProviderStateError(f"invalid {context}")
        result[key] = item
    return result


@dataclass
class _FakeVolume:
    volume_id: str
    name: str
    size_gb: int
    status: str = "available"
    metadata: dict[str, str] = field(default_factory=dict)

    def as_provider(self) -> ProviderVolume:
        return ProviderVolume(
            volume_id=self.volume_id,
            name=self.name,
            size_gb=self.size_gb,
            status=self.status,
            tags=dict(self.metadata),
        )


@dataclass
class _FakeServer:
    server_id: str
    name: str
    status: str = "ACTIVE"
    public_ipv4: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def as_provider(self) -> ProviderServer:
        return ProviderServer(
            server_id=self.server_id,
            name=self.name,
            status=self.status,
            public_ipv4=self.public_ipv4,
            tags=dict(self.metadata),
        )


class FakeProvider:
    """Behavioral twin of the provider client used by unit tests."""

    def __init__(self) -> None:
        self.volumes: dict[str, _FakeVolume] = {}
        self.servers: dict[str, _FakeServer] = {}
        self.server_volume_links: dict[str, str] = {}

    # -- volumes -----------------------------------------------------------

    def list_volumes(self) -> list[ProviderVolume]:
        return [v.as_provider() for v in self.volumes.values()]

    def get_volume(self, volume_id: str) -> ProviderVolume:
        volume = self.volumes.get(volume_id)
        if volume is None:
            raise KeyError(volume_id)
        return volume.as_provider()

    def create_volume(
        self, name: str, size_gb: int, metadata: dict[str, str]
    ) -> ProviderVolume:
        volume = _FakeVolume(
            volume_id=str(uuid.uuid4()),
            name=name,
            size_gb=size_gb,
            metadata=dict(metadata),
        )
        self.volumes[volume.volume_id] = volume
        return volume.as_provider()

    def delete_volume(self, volume_id: str) -> None:
        if volume_id not in self.volumes:
            raise KeyError(volume_id)
        if self.server_volume_links.get(volume_id) is not None:
            raise RuntimeError("volume is attached to a server")
        del self.volumes[volume_id]

    # -- servers -----------------------------------------------------------

    def list_servers(self) -> list[ProviderServer]:
        return [s.as_provider() for s in self.servers.values()]

    def get_server(self, server_id: str) -> ProviderServer:
        server = self.servers.get(server_id)
        if server is None:
            raise KeyError(server_id)
        return server.as_provider()

    def create_server(
        self,
        name: str,
        *,
        image_ref: str,
        flavor_ref: str,
        volume_id: str,
        metadata: dict[str, str],
    ) -> ProviderServer:
        if volume_id not in self.volumes:
            raise KeyError(volume_id)
        server = _FakeServer(
            server_id=str(uuid.uuid4()),
            name=name,
            status="ACTIVE",
            public_ipv4="198.51.100.7",
            metadata=dict(metadata),
        )
        self.servers[server.server_id] = server
        self.server_volume_links[volume_id] = server.server_id
        return server.as_provider()

    def delete_server(self, server_id: str) -> None:
        server = self.servers.get(server_id)
        if server is None:
            raise KeyError(server_id)
        del self.servers[server_id]
        for volume_id, linked in list(self.server_volume_links.items()):
            if linked == server_id:
                self.server_volume_links.pop(volume_id, None)

    # -- managed inventory -------------------------------------------------

    def list_managed_resources(self) -> list[ManagedResource]:
        result: list[ManagedResource] = []
        for volume in self.volumes.values():
            if volume.metadata.get("irlight-managed") != "true":
                continue
            result.append(
                ManagedResource(
                    kind="volume",
                    provider_id=volume.volume_id,
                    session_id=volume.metadata.get("irlight-session-id"),
                    user_id=volume.metadata.get("irlight-user-id"),
                    created_at=managed_since(volume.metadata),
                    delete_after=_parse_delete_after(volume.metadata),
                    details={
                        "name": volume.name,
                        "size_gb": volume.size_gb,
                        "status": volume.status,
                    },
                )
            )
        for server in self.servers.values():
            if server.metadata.get("irlight-managed") != "true":
                continue
            result.append(
                ManagedResource(
                    kind="server",
                    provider_id=server.server_id,
                    session_id=server.metadata.get("irlight-session-id"),
                    user_id=server.metadata.get("irlight-user-id"),
                    created_at=managed_since(server.metadata),
                    delete_after=_parse_delete_after(server.metadata),
                    details={
                        "name": server.name,
                        "status": server.status,
                        "public_ipv4": server.public_ipv4,
                    },
                )
            )
        return result


class FileFakeProvider(FakeProvider):
    """Fake provider whose resource tables survive across CLI invocations."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self._load()

    @staticmethod
    def _decode_state(
        state: Any,
    ) -> tuple[
        dict[str, _FakeVolume],
        dict[str, _FakeServer],
        dict[str, str],
    ]:
        if not isinstance(state, dict):
            raise FakeProviderStateError("invalid fake provider state payload")

        raw_volumes = state.get("volumes", [])
        raw_servers = state.get("servers", [])
        raw_links = state.get("server_volume_links", {})
        if not isinstance(raw_volumes, list) or not isinstance(raw_servers, list):
            raise FakeProviderStateError("invalid fake provider state payload")

        volumes: dict[str, _FakeVolume] = {}
        for item in raw_volumes:
            if not isinstance(item, dict):
                raise FakeProviderStateError("invalid fake provider volume record")
            volume_id = _require_string(
                item.get("volume_id"), context="fake provider volume id"
            )
            name = _require_string(item.get("name"), context="fake provider volume name")
            size_gb = item.get("size_gb")
            if isinstance(size_gb, bool) or not isinstance(size_gb, int):
                raise FakeProviderStateError("invalid fake provider volume size")
            status = _require_string(
                item.get("status", "available"), context="fake provider volume status"
            )
            metadata = _require_string_map(
                item.get("metadata", {}), context="fake provider volume metadata"
            )
            if volume_id in volumes:
                raise FakeProviderStateError("duplicate fake provider volume id")
            volumes[volume_id] = _FakeVolume(
                volume_id=volume_id,
                name=name,
                size_gb=size_gb,
                status=status,
                metadata=metadata,
            )

        servers: dict[str, _FakeServer] = {}
        for item in raw_servers:
            if not isinstance(item, dict):
                raise FakeProviderStateError("invalid fake provider server record")
            server_id = _require_string(
                item.get("server_id"), context="fake provider server id"
            )
            name = _require_string(item.get("name"), context="fake provider server name")
            status = _require_string(
                item.get("status", "ACTIVE"), context="fake provider server status"
            )
            public_ipv4 = item.get("public_ipv4")
            if public_ipv4 is not None and not isinstance(public_ipv4, str):
                raise FakeProviderStateError("invalid fake provider public IPv4")
            metadata = _require_string_map(
                item.get("metadata", {}), context="fake provider server metadata"
            )
            if server_id in servers:
                raise FakeProviderStateError("duplicate fake provider server id")
            servers[server_id] = _FakeServer(
                server_id=server_id,
                name=name,
                status=status,
                public_ipv4=public_ipv4,
                metadata=metadata,
            )

        links = _require_string_map(raw_links, context="fake provider server volume links")
        return volumes, servers, links

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                state = json.load(
                    handle,
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_reject_duplicate_object_pairs,
                )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise FakeProviderStateError(
                f"cannot read fake provider state {self.path}"
            ) from exc

        volumes, servers, links = self._decode_state(state)
        self.volumes = volumes
        self.servers = servers
        self.server_volume_links = links

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "volumes": [
                {
                    "volume_id": v.volume_id,
                    "name": v.name,
                    "size_gb": v.size_gb,
                    "status": v.status,
                    "metadata": v.metadata,
                }
                for v in self.volumes.values()
            ],
            "servers": [
                {
                    "server_id": s.server_id,
                    "name": s.name,
                    "status": s.status,
                    "public_ipv4": s.public_ipv4,
                    "metadata": s.metadata,
                }
                for s in self.servers.values()
            ],
            "server_volume_links": dict(self.server_volume_links),
        }
        self._decode_state(state)
        fd, temporary = _tempfile_for(self.path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                try:
                    json.dump(
                        state,
                        handle,
                        sort_keys=True,
                        allow_nan=False,
                    )
                except (TypeError, ValueError) as exc:
                    raise FakeProviderStateError(
                        "fake provider state cannot be serialized"
                    ) from exc
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def create_volume(
        self, name: str, size_gb: int, metadata: dict[str, str]
    ) -> ProviderVolume:
        volume = super().create_volume(name, size_gb, metadata)
        self._save()
        return volume

    def delete_volume(self, volume_id: str) -> None:
        super().delete_volume(volume_id)
        self._save()

    def create_server(
        self,
        name: str,
        *,
        image_ref: str,
        flavor_ref: str,
        volume_id: str,
        metadata: dict[str, str],
    ) -> ProviderServer:
        server = super().create_server(
            name,
            image_ref=image_ref,
            flavor_ref=flavor_ref,
            volume_id=volume_id,
            metadata=metadata,
        )
        self._save()
        return server

    def delete_server(self, server_id: str) -> None:
        super().delete_server(server_id)
        self._save()


def _tempfile_for(path: Path) -> tuple[int, str]:
    import tempfile

    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    return fd, temporary


def _parse_delete_after(metadata: dict[str, str]) -> float | None:
    raw = metadata.get("irlight-delete-after")
    if not raw:
        return None
    try:
        return parse_timestamp(raw)
    except (ValueError, TypeError):
        return None
