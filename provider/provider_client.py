"""Minimal ConoHa VPS v3 REST client.

ConoHa exposes OpenStack-compatible Identity/Compute/Volume endpoints.
The client keeps credentials out of logs and never reuses an expired token.
Real API calls are exercised via the admin CLI against the documented
`CONOHA_*` environment variables (see docs/cleanup-proof.md).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from provider.conoha import (
    ManagedResource,
    ProviderServer,
    ProviderVolume,
    parse_timestamp,
)


class ConohaError(RuntimeError):
    """Raised when ConoHa returns a non-success status."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"ConoHa API error (HTTP {status}): {body[:500]}")


@dataclass(frozen=True)
class ConohaConfig:
    identity_endpoint: str
    compute_endpoint: str
    volume_endpoint: str
    username: str
    password: str
    tenant_name: str
    region: str = "tyo1"
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "ConohaConfig":
        missing = [
            name
            for name in (
                "CONOHA_IDENTITY_ENDPOINT",
                "CONOHA_COMPUTE_ENDPOINT",
                "CONOHA_VOLUME_ENDPOINT",
                "CONOHA_USERNAME",
                "CONOHA_PASSWORD",
                "CONOHA_TENANT_NAME",
            )
            if not os.getenv(name)
        ]
        if missing:
            names = ", ".join(missing)
            raise RuntimeError(f"missing ConoHa env vars: {names}")
        return cls(
            identity_endpoint=os.environ["CONOHA_IDENTITY_ENDPOINT"],
            compute_endpoint=os.environ["CONOHA_COMPUTE_ENDPOINT"],
            volume_endpoint=os.environ["CONOHA_VOLUME_ENDPOINT"],
            username=os.environ["CONOHA_USERNAME"],
            password=os.environ["CONOHA_PASSWORD"],
            tenant_name=os.environ["CONOHA_TENANT_NAME"],
            region=os.getenv("CONOHA_REGION", "tyo1"),
            timeout_seconds=float(os.getenv("CONOHA_TIMEOUT_SECONDS", "30")),
        )


class ConohaClient:
    """Thread-unsafe by design; create one per admin invocation."""

    def __init__(self, config: ConohaConfig) -> None:
        self._config = config
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # -- auth -------------------------------------------------------------

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        payload = {
            "auth": {
                "passwordCredentials": {
                    "username": self._config.username,
                    "password": self._config.password,
                },
                "tenantName": self._config.tenant_name,
            }
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        data = json.dumps(payload).encode("utf-8")
        response = self._request(
            "POST",
            self._config.identity_endpoint + "/tokens",
            headers=headers,
            data=data,
            authenticated=False,
        )
        token_info = response["access"]["token"]
        self._token = token_info["id"]
        expires_at = token_info.get("expires")
        if expires_at:
            try:
                self._token_expires_at = parse_timestamp(expires_at)
            except ValueError:
                self._token_expires_at = time.time() + 3600
        else:
            self._token_expires_at = time.time() + 3600
        return self._token

    # -- low-level --------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        authenticated: bool = True,
    ) -> Any:
        merged = dict(headers or {})
        if authenticated:
            merged["X-Auth-Token"] = self._ensure_token()
        request = urllib.request.Request(url, data=data, headers=merged, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout_seconds) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            raise ConohaError(exc.code, body) from exc
        except urllib.error.URLError as exc:
            raise ConohaError(-1, f"network error: {exc.reason}") from exc

    # -- volumes ----------------------------------------------------------

    def _paginated_items(
        self,
        *,
        endpoint: str,
        path: str,
        item_key: str,
        links_key: str,
    ) -> list[dict[str, Any]]:
        """Read every OpenStack page without following cross-origin links."""
        current = endpoint.rstrip("/") + path
        endpoint_parts = urllib.parse.urlsplit(endpoint)
        endpoint_path = endpoint_parts.path.rstrip("/")
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for _page in range(1000):
            if current in seen:
                raise ConohaError(-1, "pagination loop detected")
            seen.add(current)
            data = self._request("GET", current)
            if not isinstance(data, dict) or not isinstance(data.get(item_key, []), list):
                raise ConohaError(-1, f"invalid {item_key} list response")
            for item in data.get(item_key, []):
                if not isinstance(item, dict):
                    raise ConohaError(-1, f"invalid {item_key} list item")
                result.append(item)

            next_href = None
            links = data.get(links_key, [])
            if not isinstance(links, list):
                raise ConohaError(-1, f"invalid {links_key} response")
            for link in links:
                if isinstance(link, dict) and link.get("rel") == "next":
                    next_href = link.get("href")
                    break
            if next_href is None:
                return result
            if not isinstance(next_href, str) or not next_href:
                raise ConohaError(-1, "invalid pagination URL")
            candidate = urllib.parse.urljoin(current, next_href)
            candidate_parts = urllib.parse.urlsplit(candidate)
            if (
                candidate_parts.scheme != endpoint_parts.scheme
                or candidate_parts.netloc != endpoint_parts.netloc
                or not (
                    candidate_parts.path == endpoint_path
                    or candidate_parts.path.startswith(endpoint_path + "/")
                )
            ):
                raise ConohaError(-1, "cross-origin pagination URL refused")
            current = candidate
        raise ConohaError(-1, "pagination limit exceeded")

    def list_volumes(self) -> list[ProviderVolume]:
        volumes = self._paginated_items(
            endpoint=self._config.volume_endpoint,
            path="/volumes/detail",
            item_key="volumes",
            links_key="volumes_links",
        )
        return [
            ProviderVolume(
                volume_id=str(item["id"]),
                name=str(item.get("display_name", item.get("name", ""))),
                size_gb=int(item.get("size", 0)),
                status=str(item.get("status", "")),
                tags={str(k): str(v) for k, v in (item.get("metadata") or {}).items()},
            )
            for item in volumes
        ]

    def get_volume(self, volume_id: str) -> ProviderVolume:
        data = self._request(
            "GET", f"{self._config.volume_endpoint}/volumes/{volume_id}"
        )
        item = data["volume"]
        return ProviderVolume(
            volume_id=str(item["id"]),
            name=str(item.get("name", "")),
            size_gb=int(item.get("size", 0)),
            status=str(item.get("status", "")),
            tags={str(k): str(v) for k, v in (item.get("metadata") or {}).items()},
        )

    def create_volume(
        self, name: str, size_gb: int, metadata: dict[str, str]
    ) -> ProviderVolume:
        payload = {
            "volume": {
                "size": size_gb,
                "display_name": name,
                "metadata": metadata,
            }
        }
        data = self._request(
            "POST",
            self._config.volume_endpoint + "/volumes",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload).encode("utf-8"),
        )
        item = data["volume"]
        return ProviderVolume(
            volume_id=str(item["id"]),
            name=str(item.get("display_name", item.get("name", name))),
            size_gb=int(item.get("size", size_gb)),
            status=str(item.get("status", "")),
            tags={str(k): str(v) for k, v in (item.get("metadata") or metadata).items()},
        )

    def delete_volume(self, volume_id: str) -> None:
        self._request("DELETE", f"{self._config.volume_endpoint}/volumes/{volume_id}")

    # -- servers ----------------------------------------------------------

    def list_servers(self) -> list[ProviderServer]:
        servers = self._paginated_items(
            endpoint=self._config.compute_endpoint,
            path="/servers/detail",
            item_key="servers",
            links_key="servers_links",
        )
        return [self._server_from_item(item) for item in servers]

    @staticmethod
    def _server_from_item(item: dict[str, Any]) -> ProviderServer:
        addresses = item.get("addresses") or {}
        public_ipv4 = None
        if isinstance(addresses, dict):
            for networks in addresses.values():
                if not isinstance(networks, list):
                    continue
                for address in networks:
                    if (
                        isinstance(address, dict)
                        and address.get("version") == 4
                        and address.get("addr")
                    ):
                        public_ipv4 = str(address["addr"])
                        break
                if public_ipv4:
                    break
        return ProviderServer(
            server_id=str(item["id"]),
            name=str(item.get("name", "")),
            status=str(item.get("status", "")),
            public_ipv4=public_ipv4,
            tags={str(k): str(v) for k, v in (item.get("metadata") or {}).items()},
        )

    def get_server(self, server_id: str) -> ProviderServer:
        data = self._request(
            "GET", f"{self._config.compute_endpoint}/servers/{server_id}"
        )
        return self._server_from_item(data["server"])

    def create_server(
        self,
        name: str,
        *,
        image_ref: str,
        flavor_ref: str,
        volume_id: str,
        metadata: dict[str, str],
    ) -> ProviderServer:
        payload = {
            "server": {
                "name": name,
                "imageRef": image_ref,
                "flavorRef": flavor_ref,
                "block_device_mapping_v2": [
                    {
                        "uuid": volume_id,
                        "source_type": "volume",
                        "destination_type": "volume",
                        "boot_index": 0,
                        "delete_on_termination": False,
                    }
                ],
                "metadata": metadata,
            }
        }
        data = self._request(
            "POST",
            self._config.compute_endpoint + "/servers",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload).encode("utf-8"),
        )
        item = data["server"]
        return ProviderServer(
            server_id=str(item["id"]),
            name=str(item.get("name", name)),
            status=str(item.get("status", "")),
            tags={str(k): str(v) for k, v in (item.get("metadata") or metadata).items()},
        )

    def delete_server(self, server_id: str) -> None:
        self._request("DELETE", f"{self._config.compute_endpoint}/servers/{server_id}")

    # -- managed inventory -------------------------------------------------

    def list_managed_resources(self) -> list[ManagedResource]:
        from provider.conoha import managed_since

        result: list[ManagedResource] = []
        for volume in self.list_volumes():
            if not volume.is_managed:
                continue
            result.append(
                ManagedResource(
                    kind="volume",
                    provider_id=volume.volume_id,
                    session_id=volume.tags.get("irlight-session-id"),
                    user_id=volume.tags.get("irlight-user-id"),
                    created_at=managed_since(volume.tags),
                    delete_after=parse_delete_after(volume.tags),
                    details={"name": volume.name, "size_gb": volume.size_gb, "status": volume.status},
                )
            )
        for server in self.list_servers():
            if not server.is_managed:
                continue
            result.append(
                ManagedResource(
                    kind="server",
                    provider_id=server.server_id,
                    session_id=server.tags.get("irlight-session-id"),
                    user_id=server.tags.get("irlight-user-id"),
                    created_at=managed_since(server.tags),
                    delete_after=parse_delete_after(server.tags),
                    details={
                        "name": server.name,
                        "status": server.status,
                        "public_ipv4": server.public_ipv4,
                    },
                )
            )
        return result


def parse_delete_after(tags: dict[str, str]) -> float | None:
    raw = tags.get("irlight-delete-after")
    if not raw:
        return None
    try:
        return parse_timestamp(raw)
    except (ValueError, TypeError):
        return None
