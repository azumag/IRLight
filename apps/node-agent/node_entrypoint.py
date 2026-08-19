"""Node Agent entrypoint that keeps MediaMTX auth local to the Media Node."""

from __future__ import annotations

import os

from agent import main as agent_main
from ingest_auth_proxy import AuthProxyConfig, IngestAuthProxy


def main() -> int:
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
