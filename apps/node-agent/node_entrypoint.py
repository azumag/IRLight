"""Node Agent entrypoint that keeps MediaMTX auth local to the Media Node."""

from __future__ import annotations

import os
from pathlib import Path

from agent import NodeAgent, main as agent_main
from continuity_status import gate_ingest_observation, read_continuity_status
from ingest_auth_proxy import AuthProxyConfig, IngestAuthProxy


def _install_continuity_gate() -> None:
    status_file = os.getenv("NODE_CONTINUITY_STATUS_FILE", "").strip()
    if not status_file or getattr(NodeAgent, "_continuity_gate_installed", False):
        return

    path = Path(status_file)
    original = NodeAgent._ingest_observation

    def gated_ingest(self: NodeAgent) -> dict[str, object] | None:
        observation = original(self)
        continuity = read_continuity_status(path)
        return gate_ingest_observation(observation, continuity)

    NodeAgent._ingest_observation = gated_ingest  # type: ignore[method-assign]
    NodeAgent._continuity_gate_installed = True  # type: ignore[attr-defined]


def main() -> int:
    _install_continuity_gate()
    if os.getenv("NODE_INGEST_AUTH_PROXY_ENABLED", "1") == "0":
        return agent_main()

    control_base_url = os.getenv("NODE_CONTROL_PLANE_URL", "").rstrip("/")
    if not control_base_url:
        raise RuntimeError("missing required env var: NODE_CONTROL_PLANE_URL")
    upstream_url = os.getenv("NODE_INGEST_AUTH_UPSTREAM_URL", "").strip()
    if not upstream_url:
        upstream_url = f"{control_base_url}/internal/ingest/auth"

    proxy = IngestAuthProxy(
        upstream_url=upstream_url,
        config=AuthProxyConfig.from_env(),
    )
    proxy.start()
    try:
        return agent_main()
    finally:
        proxy.stop()


if __name__ == "__main__":
    raise SystemExit(main())
