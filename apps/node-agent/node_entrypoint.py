"""Node Agent entrypoint that keeps MediaMTX auth local to the Media Node."""

from __future__ import annotations

import os
import secrets
import tempfile
import urllib.parse
from pathlib import Path

from agent import main as agent_main
from ingest_auth_proxy import (
    INTERNAL_MEDIA_USERNAME,
    INTERNAL_MEDIA_ACTIONS,
    AuthProxyConfig,
    IngestAuthProxy,
)


INTERNAL_SECRET_FILES = (
    "media_input_uri",
    "media_publish_uri",
    "media_relay_uri",
)


def _atomic_write_secret(path: Path, value: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def prepare_internal_media_auth(
    secret_dir: Path,
    relay_secret_dir: Path | None = None,
) -> tuple[dict[tuple[str, str, str], str], list[Path]]:
    """Create node-local media credentials in the configured tmpfs."""
    secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    secret_dir.chmod(0o700)
    relay_secret_dir = relay_secret_dir or secret_dir
    relay_secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    relay_secret_dir.chmod(0o700)
    encoded_user = urllib.parse.quote(INTERNAL_MEDIA_USERNAME, safe="")
    action_secrets = {action: secrets.token_urlsafe(32) for action in INTERNAL_MEDIA_ACTIONS}
    input_secret = urllib.parse.quote(
        action_secrets[("rtsp", "read", "live/input")], safe=""
    )
    publish_secret = urllib.parse.quote(
        action_secrets[("rtmp", "publish", "output/relay")], safe=""
    )
    relay_secret = urllib.parse.quote(
        action_secrets[("rtsp", "read", "output/relay")], safe=""
    )
    values = {
        "media_input_uri": (
            f"rtsp://{encoded_user}:{input_secret}@mediamtx:8554/live/input"
        ),
        "media_publish_uri": (
            "rtmp://mediamtx:1935/output/relay"
            f"?user={encoded_user}&pass={publish_secret}"
        ),
        "media_relay_uri": (
            f"rtsp://{encoded_user}:{relay_secret}@mediamtx:8554/output/relay"
        ),
    }
    paths = [
        secret_dir / "media_input_uri",
        secret_dir / "media_publish_uri",
        relay_secret_dir / "media_relay_uri",
    ]
    try:
        for name, value in values.items():
            path = (
                relay_secret_dir / name
                if name == "media_relay_uri"
                else secret_dir / name
            )
            _atomic_write_secret(path, value)
    except Exception:
        remove_internal_media_auth(paths)
        raise
    return action_secrets, paths


def remove_internal_media_auth(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


class InternalMediaAuthLifecycle:
    """Start local auth only after bootstrap and clean every partial start."""

    def __init__(
        self, *, upstream_url: str, secret_dir: Path, relay_secret_dir: Path
    ) -> None:
        self.upstream_url = upstream_url
        self.secret_dir = secret_dir
        self.relay_secret_dir = relay_secret_dir
        self.paths: list[Path] = []
        self.proxy: IngestAuthProxy | None = None

    def start(self, node_access_token: str) -> None:
        if not node_access_token:
            raise RuntimeError("missing Node access token for ingest auth upstream")
        action_secrets, self.paths = prepare_internal_media_auth(
            self.secret_dir, self.relay_secret_dir
        )
        self.proxy = IngestAuthProxy(
            upstream_url=self.upstream_url,
            config=AuthProxyConfig.from_env(),
            upstream_token=node_access_token,
            internal_media_secrets=action_secrets,
        )
        try:
            self.proxy.start()
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        if self.proxy is not None:
            try:
                self.proxy.stop()
            finally:
                self.proxy = None
        remove_internal_media_auth(self.paths)
        self.paths = []


def main() -> int:
    if os.getenv("NODE_INGEST_AUTH_PROXY_ENABLED", "1") == "0":
        return agent_main()

    control_base_url = os.getenv("NODE_CONTROL_PLANE_URL", "").rstrip("/")
    if not control_base_url:
        raise RuntimeError("missing required env var: NODE_CONTROL_PLANE_URL")
    upstream_url = os.getenv("NODE_INGEST_AUTH_UPSTREAM_URL", "").strip()
    if not upstream_url:
        upstream_url = f"{control_base_url}/internal/ingest/auth"

    secret_dir = Path(
        os.getenv("NODE_MEDIA_SECRET_DIR")
        or os.getenv("NODE_SECRET_DIR")
        or "/run/irlight/media-secrets"
    )
    relay_secret_dir = Path(
        os.getenv("NODE_RELAY_SECRET_DIR")
        or os.getenv("NODE_MEDIA_SECRET_DIR")
        or os.getenv("NODE_SECRET_DIR")
        or "/run/irlight/relay-secrets"
    )
    lifecycle = InternalMediaAuthLifecycle(
        upstream_url=upstream_url,
        secret_dir=secret_dir,
        relay_secret_dir=relay_secret_dir,
    )
    return agent_main(
        pre_media_start=lifecycle.start,
        post_media_stop=lifecycle.stop,
    )


if __name__ == "__main__":
    raise SystemExit(main())
