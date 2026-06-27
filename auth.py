"""Authentication / authorization for gemini2api.

The server exposes two distinct trust zones:

* **OpenAI-compatible API** (``/v1/*``) — protected by ``API_KEY`` via
  ``Authorization: Bearer <key>`` headers. Compatible with every OpenAI client
  by setting ``api_key``/``OPENAI_API_KEY`` to the same value.

* **Admin / WebUI** (``/api/*`` and the HTML pages) — protected by
  ``ADMIN_KEY`` via either the same ``Authorization: Bearer`` header *or* a
  signed cookie (``admin_token``). The cookie path lets a human open the
  dashboard in a browser once without re-pasting the key on every request.

Setting either variable to the placeholder value (or to an empty string)
**disables** enforcement — handy for local dev, never recommended in prod.
"""
from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Optional

from fastapi import Cookie, Header, HTTPException, Request, status

from logger import get_logger

log = get_logger("auth")

# Identifiers the codebase uses to mean "no real key configured".
_PLACEHOLDER = "sk-web2api-placeholder"
_DISABLE_SENTINELS = frozenset({"", _PLACEHOLDER, "disabled", "off"})


@dataclass(frozen=True)
class AuthConfig:
    """Effective auth settings after resolving env vars."""

    api_key: Optional[str]
    admin_key: Optional[str]
    api_required: bool
    admin_required: bool

    @property
    def api_status(self) -> str:
        return "required" if self.api_required else "disabled"

    @property
    def admin_status(self) -> str:
        return "required" if self.admin_required else "disabled"


def _is_disabled(raw: Optional[str]) -> bool:
    return raw is None or raw.strip().lower() in _DISABLE_SENTINELS


def load_auth_config() -> AuthConfig:
    """Read ``API_KEY`` / ``ADMIN_KEY`` from env and resolve policy."""
    api_key_raw = os.getenv("API_KEY")
    admin_key_raw = os.getenv("ADMIN_KEY", api_key_raw)
    api_required = not _is_disabled(api_key_raw)
    admin_required = not _is_disabled(admin_key_raw)
    return AuthConfig(
        api_key=api_key_raw if api_required else None,
        admin_key=admin_key_raw if admin_required else None,
        api_required=api_required,
        admin_required=admin_required,
    )


# Module-level singleton, refreshed on server start.
CONFIG = load_auth_config()


def _safe_compare(provided: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time comparison; tolerates ``None`` expected."""
    if expected is None or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


# ── FastAPI dependencies ─────────────────────────────────────────────

def verify_api_key(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
) -> None:
    """Dependency guarding ``/v1/*`` endpoints.

    Accepts the key from ``Authorization: Bearer <key>`` or the legacy
    ``x-api-key`` header. Returns 401 on mismatch.
    """
    if not CONFIG.api_required:
        return  # Auth disabled — no-op.

    provided = _extract_bearer(authorization) or x_api_key
    if not _safe_compare(provided, CONFIG.api_key):
        log.warning(
            "api_key_invalid",
            extra={"client": request.client.host if request.client else "?"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"type": "invalid_api_key", "message": "Invalid or missing API key"}},
            headers={"WWW-Authenticate": 'Bearer realm="gemini2api"'},
        )


def verify_admin_key(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    admin_token: Optional[str] = Cookie(default=None),
) -> None:
    """Dependency guarding the WebUI and admin REST API.

    Accepts the key from ``Authorization: Bearer <key>`` or the ``admin_token``
    cookie set by ``POST /api/auth/web_login``. When the request is for an
    HTML page and auth fails, the response redirects to ``/webui/login``
    instead of returning a JSON 401.
    """
    if not CONFIG.admin_required:
        return  # Auth disabled — no-op.

    provided = _extract_bearer(authorization) or admin_token
    if _safe_compare(provided, CONFIG.admin_key):
        return
    log.warning(
        "admin_key_invalid",
        extra={"path": request.url.path, "client": request.client.host if request.client else "?"},
    )
    # WebUI page requests get a friendly redirect; everything else gets JSON.
    path = request.url.path
    if path.startswith("/webui") or path == "/" or path.endswith(".html"):
        from fastapi.responses import RedirectResponse
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            detail="redirect to login",
            headers={"Location": "/webui/login"},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": {"type": "invalid_admin_key", "message": "Invalid or missing admin key"}},
    )


def check_admin_login(provided: str) -> bool:
    """Plain helper used by ``/api/auth/login`` to validate submitted keys."""
    if not CONFIG.admin_required:
        return True
    return _safe_compare(provided, CONFIG.admin_key)


def auth_summary() -> dict:
    """Return a non-sensitive snapshot of the auth state for the dashboard."""
    return {
        "api_key_status": CONFIG.api_status,
        "admin_key_status": CONFIG.admin_status,
    }
