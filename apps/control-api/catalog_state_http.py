"""Stable HTTP mapping for catalog authority failures.

Catalog state corruption or I/O failure is an availability problem, not a
client validation error. Keep the public response deliberately coarse so raw
state paths, parser details, Destination metadata, and secret references never
escape through an unhandled exception.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from catalog_store import CatalogStateError


CATALOG_STATE_UNAVAILABLE_CODE = "CATALOG_STATE_UNAVAILABLE"


def catalog_state_unavailable(
    _request: Request,
    _exc: CatalogStateError,
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": {"code": CATALOG_STATE_UNAVAILABLE_CODE}},
    )


def install_catalog_state_error_handler(app: FastAPI) -> None:
    """Map any runtime CatalogStateError escaping a route to a stable 503."""

    app.add_exception_handler(CatalogStateError, catalog_state_unavailable)
