from __future__ import annotations

import json
import os
import tempfile
import threading
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
from node_internal import ensure_state as ensure_node_state
from node_internal import router as node_internal_router
from session_api import router as session_router


STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
CONTROL_PATH = STATE_DIR / "control.json"
STATUS_PATH = STATE_DIR / "status.json"
LOCK = threading.Lock()


class AudioCommand(BaseModel):
    mode: Literal["LIVE", "MUTED"]
    expected_version: int | None = Field(default=None, ge=0)


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_json(path: Path, default: dict[str, object]) -> dict[str, object]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else default
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def default_control() -> dict[str, object]:
    return {
        "audio_mode": "LIVE",
        "version": 0,
        "command_id": None,
        "idempotency_key": None,
        "updated_at": time.time(),
    }


def ensure_control() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not CONTROL_PATH.exists():
        atomic_write_json(CONTROL_PATH, default_control())


ensure_control()
ensure_node_state()
ensure_catalog()
ensure_auth_state()
app = FastAPI(title="IRLight Phase 0 Control API", version="0.1.0")
app.include_router(node_internal_router)
app.include_router(auth_router)
app.include_router(catalog_router)
app.include_router(session_router)


@app.get("/api/status")
def get_status() -> dict[str, object]:
    control = read_json(CONTROL_PATH, default_control())
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
    key = idempotency_key or str(uuid.uuid4())
    if len(key) > 200:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long")

    with LOCK:
        current = read_json(CONTROL_PATH, default_control())
        current_version = int(current.get("version", 0))

        if current.get("idempotency_key") == key:
            if current.get("audio_mode") != command.mode:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key was already used for another mode",
                )
            return current

        if (
            command.expected_version is not None
            and command.expected_version != current_version
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "VERSION_CONFLICT",
                    "current_version": current_version,
                    "current_mode": current.get("audio_mode", "LIVE"),
                },
            )

        next_control: dict[str, object] = {
            "audio_mode": command.mode,
            "version": current_version + 1,
            "command_id": str(uuid.uuid4()),
            "idempotency_key": key,
            "updated_at": time.time(),
        }
        atomic_write_json(CONTROL_PATH, next_control)
        return next_control


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")