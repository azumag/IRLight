"""User authentication API: register / login / logout / me.

Session identity is carried in an HttpOnly, Secure, SameSite=Lax cookie so it
is never readable from page JavaScript. CSRF protection uses the
double-submit pattern: a second, non-HttpOnly cookie carries the same-session
CSRF token, and every state-changing request must echo it back in the
`X-CSRF-Token` header. A cross-site page can make the browser send cookies,
but it cannot read the CSRF cookie's value to put it in the header, so the
check fails for forged requests while same-site requests pass it trivially.

`require_user` is the dependency other routers (catalog_api, session_api)
use to resolve the authenticated user and stop trusting a client-supplied
`user_id`. `require_csrf` is the dependency mutating endpoints add.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field

from auth_store import (
    AuthError,
    EmailAlreadyRegistered,
    InvalidCredentials,
    authenticate_user,
    create_session,
    get_session_user,
    register_user,
    revoke_session,
)


SESSION_COOKIE = "irlight_session"
CSRF_COOKIE = "irlight_csrf"
SESSION_TTL_SECONDS = 7 * 24 * 3600

# A simple, dependency-free "looks like an email" check; real deliverability
# is out of scope here and would need an external service.
_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=_EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=200)
    display_name: str | None = Field(default=None, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=_EMAIL_PATTERN)
    password: str = Field(min_length=1, max_length=200)


router = APIRouter(prefix="/v1/auth")


def _cookies_secure() -> bool:
    # Local/dev docker-compose runs over plain HTTP; production always sets
    # this back to secure cookies via the environment.
    return os.getenv("COOKIE_INSECURE", "") != "1"


def _set_session_cookies(response: Response, token: str, csrf_token: str) -> None:
    secure = _cookies_secure()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=SESSION_TTL_SECONDS,
        path="/",
        httponly=False,
        secure=secure,
        samesite="lax",
    )


def require_user(
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> dict[str, Any]:
    if not session_token:
        raise HTTPException(status_code=401, detail="not authenticated")
    session = get_session_user(session_token)
    if session is None:
        raise HTTPException(status_code=401, detail="session expired or invalid")
    return session["user"]


def require_csrf(
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf_cookie: Annotated[str | None, Cookie(alias=CSRF_COOKIE)] = None,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    if not session_token:
        raise HTTPException(status_code=401, detail="not authenticated")
    session = get_session_user(session_token)
    if session is None:
        raise HTTPException(status_code=401, detail="session expired or invalid")
    if (
        not csrf_cookie
        or not x_csrf_token
        or csrf_cookie != x_csrf_token
        or csrf_cookie != session["csrf_token"]
    ):
        raise HTTPException(status_code=403, detail="missing or invalid CSRF token")


@router.post("/register")
def register(request: RegisterRequest) -> dict[str, Any]:
    try:
        user = register_user(
            email=request.email,
            password=request.password,
            display_name=request.display_name,
        )
    except EmailAlreadyRegistered as exc:
        raise HTTPException(status_code=409, detail="email already registered") from exc
    except AuthError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"user": user}


@router.post("/login")
def login(request: LoginRequest, response: Response) -> dict[str, Any]:
    try:
        user = authenticate_user(email=request.email, password=request.password)
    except InvalidCredentials as exc:
        raise HTTPException(status_code=401, detail="invalid email or password") from exc
    session = create_session(str(user["id"]), ttl_seconds=SESSION_TTL_SECONDS)
    _set_session_cookies(response, session["token"], session["csrf_token"])
    return {"user": user, "csrf_token": session["csrf_token"]}


@router.post("/logout")
def logout(
    response: Response,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    _csrf: Annotated[None, Depends(require_csrf)] = None,
) -> dict[str, Any]:
    if session_token:
        revoke_session(session_token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"logged_out": True}


@router.get("/me")
def me(current_user: Annotated[dict[str, Any], Depends(require_user)]) -> dict[str, Any]:
    return {"user": current_user}
