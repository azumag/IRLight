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

from provider.conoha import (
    ManagedResource,
    ProviderServer,
    ProviderVolume,
    managed_since,
    format_timestamp,
)


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

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return
        for item in state.get("volumes", []):
            volume = _FakeVolume(
                volume_id=item["volume_id"],
                name=item["name"],
                size_gb=item["size_gb"],
                status=item.get("status", "available"),
                metadata=item.get("metadata", {}),
            )
            self.volumes[volume.volume_id] = volume
        for item in state.get("servers", []):
            server = _FakeServer(
                server_id=item["server_id"],
                name=item["name"],
                status=item.get("status", "ACTIVE"),
                public_ipv4=item.get("public_ipv4"),
                metadata=item.get("metadata", {}),
            )
            self.servers[server.server_id] = server
        self.server_volume_links = dict(state.get("server_volume_links", {}))

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
        fd, temporary = _tempfile_for(self.path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True)
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
        return time.mktime(time.strptime(raw, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None
