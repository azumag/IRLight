from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from auth_api import router as auth_router
from auth_store import ensure_auth_state
from catalog_api import router as catalog_router
from catalog_store import ensure_catalog
from control_store import (
    ControlIdempotencyConflict,
    ControlStateError,
    ControlStore,
    ControlVersionConflict,
)
from ingest_api import internal_router as ingest_internal_router
from ingest_api import user_router as ingest_user_router
from node_internal import ensure_state as ensure_node_state
from node_internal import router as node_internal_router
from session_api import router as session_router


STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
CONTROL_PATH = STATE_DIR / "control.json"
STATUS_PATH = STATE_DIR / "status.json"
CONTROL_STORE = ControlStore(STATE_DIR)


class AudioCommand(BaseModel):
    mode: Literal["LIVE", "MUTED"]
    expected_version: int | None = Field(default=None, ge=0)


def read_json(path: Path, default: dict[str, object]) -> dict[str, object]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else default
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def ensure_control() -> None:
    CONTROL_STORE.ensure()


ensure_control()
ensure_node_state()
ensure_catalog()
ensure_auth_state()
app = FastAPI(title="IRLight Phase 0 Control API", version="0.1.0")
app.include_router(node_internal_router)
app.include_router(ingest_internal_router)
app.include_router(auth_router)
app.include_router(catalog_router)
app.include_router(session_router)
app.include_router(ingest_user_router)


@app.get("/api/status")
def get_status() -> dict[str, object]:
    try:
        control = CONTROL_STORE.get()
    except ControlStateError as exc:
        raise HTTPException(status_code=503, detail="control state unavailable") from exc
    runtime = read_json(
        STATUS_PATH,
        {
            "session_status": "STARTING",
            "video_source": "STANDBY",
            "actual_audio_mode": "SILENT_FALLBACK",
            "updated_at": None,
        },
    )
    return {"control": control, "runtime": runtime, "server_time": time.time()}


@app.put("/api/audio")
def set_audio(
    command: AudioCommand,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    # The original Phase-0 endpoint has no browser session/CSRF boundary.
    # Keep it available only for an explicitly enabled local PoC; production
    # callers must use the authenticated /v1 APIs.
    if os.getenv("IRLIGHT_LEGACY_AUDIO_API_ENABLED", "0") != "1":
        raise HTTPException(status_code=404, detail="legacy audio API disabled")
    key = idempotency_key or str(uuid.uuid4())
    if len(key) > 200:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long")

    try:
        return CONTROL_STORE.update(
            mode=command.mode,
            expected_version=command.expected_version,
            idempotency_key=key,
        )
    except ControlIdempotencyConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used for another mode",
        ) from exc
    except ControlVersionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VERSION_CONFLICT",
                "current_version": exc.current["version"],
                "current_mode": exc.current["audio_mode"],
            },
        ) from exc
    except ControlStateError as exc:
        raise HTTPException(status_code=503, detail="control state unavailable") from exc


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
