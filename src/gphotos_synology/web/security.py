"""Application-level authentication, DSM profile isolation, and CSRF defense (C1, C2, G6)."""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from gphotos_synology.config import Settings
    from gphotos_synology.profiles import UserProfile

logger = logging.getLogger(__name__)

DSM_USER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
SESSION_COOKIE_NAME = "gphotos_session"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Endpoints accessible without authentication (health probe, static UI shell, login)
AUTH_EXEMPT_EXACT = frozenset(
    {
        "/",
        "/api/health",
        "/api/auth/status",
        "/api/auth/session",
        "/api/auth/logout",
    }
)


@dataclass(frozen=True)
class RequestIdentity:
    """Verified caller identity attached to `request.state.identity`."""

    authenticated: bool
    via_proxy: bool
    dsm_user: str
    is_admin: bool


def load_proxy_secret(settings: Settings) -> str:
    """Resolve the shared proxy secret from env or secret file."""
    direct = settings.proxy_secret.strip()
    if direct:
        return direct
    if settings.proxy_secret_file is not None:
        try:
            if settings.proxy_secret_file.is_file():
                val = settings.proxy_secret_file.read_text(encoding="utf-8").strip()
                if val:
                    return val
        except OSError as exc:
            logger.warning("Could not read proxy secret file %s: %s", settings.proxy_secret_file, exc)
    return ""


def load_or_create_api_token(settings: Settings) -> str:
    """Resolve direct API token from env or auto-generate a 0600 token file in data_dir."""
    direct = settings.api_token.strip()
    if direct:
        return direct
    if settings.allow_insecure_no_auth:
        return ""
    if load_proxy_secret(settings):
        # SPK proxy mode uses X-Proxy-Secret; no separate direct token needed unless requested
        return ""

    token_file: Path = settings.data_dir / "api.token"
    try:
        if token_file.is_file():
            existing = token_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError:
        pass

    token_file.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(32)
    tmp = token_file.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(generated + "\n")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    try:
        os.chmod(tmp, 0o600)
    except OSError as exc:
        logger.warning("Could not enforce 0600 permissions on %s: %s", token_file, exc)
    os.replace(tmp, token_file)
    logger.info("Generated direct API token at %s", token_file)
    return generated


def parse_admin_users(raw: str) -> set[str]:
    return {u.strip().lower() for u in (raw or "").split(",") if u.strip()}


def get_identity(request: Request) -> RequestIdentity:
    identity = getattr(request.state, "identity", None)
    if isinstance(identity, RequestIdentity):
        return identity
    return RequestIdentity(authenticated=False, via_proxy=False, dsm_user="", is_admin=False)


def assert_admin(identity: RequestIdentity) -> None:
    if not identity.is_admin:
        raise HTTPException(status_code=403, detail="Administrator privileges required")


def assert_profile_access(identity: RequestIdentity, profile: UserProfile) -> None:
    """Enforce server-side per-user DSM profile isolation (C2)."""
    if identity.is_admin:
        return
    if not identity.dsm_user or profile.dsm_username.lower() != identity.dsm_user.lower():
        raise HTTPException(
            status_code=403,
            detail="Access denied: you can only access your own Synology user profile",
        )


def _extract_bearer_or_token(request: Request) -> str:
    auth_header = request.headers.get("authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    api_hdr = request.headers.get("x-api-token", "").strip()
    if api_hdr:
        return api_hdr
    cookie_tok = (request.cookies.get(SESSION_COOKIE_NAME) or "").strip()
    if cookie_tok:
        return cookie_tok
    return ""


class SecurityMiddleware(BaseHTTPMiddleware):
    """Enforces C1 (app-level auth), C2 (trusted proxy identity), and G6 (CSRF header)."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method.upper()
        app_ctx = request.app.state.ctx
        settings: Settings = app_ctx.config_manager.settings

        # 1. Static files & root HTML shell
        if path == "/" or path.startswith("/static/"):
            request.state.identity = RequestIdentity(
                authenticated=False, via_proxy=False, dsm_user="", is_admin=False
            )
            return await call_next(request)

        # 2. G6: CSRF defense — require X-Requested-With on all mutating /api/* requests
        if path.startswith("/api/") and method in MUTATING_METHODS:
            req_with = request.headers.get("x-requested-with", "").strip()
            if not req_with:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Missing required X-Requested-With CSRF header"},
                )

        # 3. Evaluate caller identity (proxy secret -> direct API token -> insecure opt-in)
        proxy_secret = load_proxy_secret(settings)
        api_token = app_ctx.effective_api_token
        provided_proxy_secret = request.headers.get("x-proxy-secret", "").strip()
        provided_token = _extract_bearer_or_token(request)
        admin_users = parse_admin_users(settings.dsm_admin_users)

        identity: RequestIdentity | None = None

        if proxy_secret and provided_proxy_secret and hmac.compare_digest(
            provided_proxy_secret, proxy_secret
        ):
            dsm_user = request.headers.get("x-dsm-user", "").strip()
            if not dsm_user or not DSM_USER_RE.match(dsm_user):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Invalid or missing X-DSM-User from authenticated proxy"},
                )
            dsm_admin_hdr = request.headers.get("x-dsm-admin", "").strip()
            is_admin = (dsm_admin_hdr == "1") or (dsm_user.lower() in admin_users)
            identity = RequestIdentity(
                authenticated=True,
                via_proxy=True,
                dsm_user=dsm_user,
                is_admin=is_admin,
            )
        elif api_token and provided_token and hmac.compare_digest(provided_token, api_token):
            identity = RequestIdentity(
                authenticated=True,
                via_proxy=False,
                dsm_user="",
                is_admin=True,
            )
        elif settings.allow_insecure_no_auth and not proxy_secret and not settings.api_token.strip():
            dsm_user = ""
            if settings.trust_proxy_headers:
                raw_user = request.headers.get("x-dsm-user", "").strip()
                if DSM_USER_RE.match(raw_user):
                    dsm_user = raw_user
            identity = RequestIdentity(
                authenticated=True,
                via_proxy=False,
                dsm_user=dsm_user,
                is_admin=True,
            )

        # 4. Exempt endpoints (still receive resolved identity if caller authenticated)
        if path in AUTH_EXEMPT_EXACT:
            request.state.identity = identity or RequestIdentity(
                authenticated=False, via_proxy=False, dsm_user="", is_admin=False
            )
            return await call_next(request)

        # 5. All other /api/* routes require authentication
        if path.startswith("/api/"):
            if identity is None or not identity.authenticated:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Authentication required"},
                )
            request.state.identity = identity
            return await call_next(request)

        request.state.identity = identity or RequestIdentity(
            authenticated=False, via_proxy=False, dsm_user="", is_admin=False
        )
        return await call_next(request)
