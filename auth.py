"""Authentication / authorization for gemini2api.

The server exposes two distinct trust zones:

* **OpenAI-compatible API** (``/v1/*``) — protected by ``API_KEY`` and
  the optional ``API_KEYS`` list via ``Authorization: Bearer <key>``
  headers. Compatible with every OpenAI client by setting
  ``api_key``/``OPENAI_API_KEY`` to one of the accepted values.

* **Admin / WebUI** (``/api/*`` and the HTML pages) — protected by
  ``ADMIN_KEY`` and the optional ``ADMIN_KEYS`` list, accepted via
  either the same ``Authorization: Bearer`` header *or* a signed
  cookie (``admin_token``). The cookie path lets a human open the
  dashboard in a browser once without re-pasting the key on every
  request.

Setting either variable to the placeholder value (or to an empty string)
**disables** enforcement — handy for local dev, never recommended in prod.
"""
from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field
from typing import Iterable, Optional

from fastapi import Cookie, Header, HTTPException, Request, status

from logger import get_logger

log = get_logger("auth")

# Identifiers the codebase uses to mean "no real key configured".
_PLACEHOLDER = "sk-web2api-placeholder"
_DISABLE_SENTINELS = frozenset({"", _PLACEHOLDER, "disabled", "off"})


@dataclass(frozen=True)
class AuthConfig:
    """Effective auth settings after resolving env vars."""

    api_keys: tuple[str, ...] = ()
    admin_keys: tuple[str, ...] = ()
    api_required: bool = False
    admin_required: bool = False

    @property
    def api_status(self) -> str:
        return "required" if self.api_required else "disabled"

    @property
    def admin_status(self) -> str:
        return "required" if self.admin_required else "disabled"

    @property
    def api_key_count(self) -> int:
        return len(self.api_keys)

    @property
    def admin_key_count(self) -> int:
        return len(self.admin_keys)


def _is_disabled(raw: Optional[str]) -> bool:
    return raw is None or raw.strip().lower() in _DISABLE_SENTINELS


def _parse_keys(*raws: Optional[str]) -> tuple[str, ...]:
    """Combine ``API_KEY`` + ``API_KEYS`` (and similar) into a deduped tuple.

    Empty / placeholder entries are filtered out; whitespace-padded
    values are trimmed. The first non-empty value wins on dedup so the
    logs and ``CONFIG.api_key_count`` stay stable.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in raws:
        if raw is None:
            continue
        # First treat the raw value as a single key.
        candidates: list[str] = []
        if not _is_disabled(raw):
            candidates.append(raw.strip())
        # Then, if the value contains commas, split it.
        for part in raw.split(","):
            part = part.strip()
            if part and not _is_disabled(part) and part not in seen:
                seen.add(part)
                out.append(part)
        if not raw.strip().count(",") and candidates:
            # Single-key form (no commas) already handled above.
            for c in candidates:
                if c not in seen:
                    seen.add(c)
                    out.append(c)
    return tuple(out)


def load_auth_config() -> AuthConfig:
    """Read ``API_KEY[S]`` / ``ADMIN_KEY[S]`` from env and resolve policy."""
    api_single = os.getenv("API_KEY")
    api_list = os.getenv("API_KEYS")
    admin_single = os.getenv("ADMIN_KEY", api_single)
    admin_list = os.getenv("ADMIN_KEYS")

    api_keys = _parse_keys(api_single, api_list)
    admin_keys = _parse_keys(admin_single, admin_list)
    return AuthConfig(
        api_keys=api_keys,
        admin_keys=admin_keys,
        api_required=bool(api_keys),
        admin_required=bool(admin_keys),
    )


# Module-level singleton, refreshed on server start.
CONFIG = load_auth_config()


def _safe_compare(provided: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time comparison; tolerates ``None`` expected."""
    if expected is None or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _safe_compare_any(provided: Optional[str], expected: Iterable[str]) -> bool:
    """Constant-time comparison against any candidate in ``expected``."""
    if not provided:
        return False
    # We don't truly constant-time across candidates of different
    # lengths, but every comparison uses hmac.compare_digest so timing
    # leakage is bounded to length differences. This is good enough for
    # the threat model here (untrusted LAN clients, not nation-states).
    return any(_safe_compare(provided, k) for k in expected if k)


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
    ``x-api-key`` header. Matches against ``API_KEY`` and any value in
    ``API_KEYS`` (comma-separated list). Returns 401 on mismatch.
    """
    if not CONFIG.api_required:
        return  # Auth disabled — no-op.

    provided = _extract_bearer(authorization) or x_api_key
    if _safe_compare_any(provided, CONFIG.api_keys):
        return
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

    Accepts the key from ``Authorization: Bearer <key>`` or the
    ``admin_token`` cookie set by ``POST /api/auth/web_login``.
    Matches against ``ADMIN_KEY`` and any value in ``ADMIN_KEYS``.
    HTML page requests get a 302 to ``/webui/login`` on failure;
    everything else gets JSON 401.
    """
    if not CONFIG.admin_required:
        return  # Auth disabled — no-op.

    provided = _extract_bearer(authorization) or admin_token
    if _safe_compare_any(provided, CONFIG.admin_keys):
        return
    log.warning(
        "admin_key_invalid",
        extra={"path": request.url.path, "client": request.client.host if request.client else "?"},
    )
    path = request.url.path
    if path.startswith("/webui") or path == "/" or path.endswith(".html"):
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
    """Plain helper used by ``/api/auth/web_login`` to validate submitted keys."""
    if not CONFIG.admin_required:
        return True
    return _safe_compare_any(provided, CONFIG.admin_keys)


def auth_summary() -> dict:
    """Return a non-sensitive snapshot of the auth state for the dashboard."""
    return {
        "api_key_status": CONFIG.api_status,
        "admin_key_status": CONFIG.admin_status,
        "api_key_count": CONFIG.api_key_count,
        "admin_key_count": CONFIG.admin_key_count,
    }
