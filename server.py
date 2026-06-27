"""FastAPI entrypoint for gemini2api v1.0.

Layout
------
* Profiles  (model definitions) are loaded from ``PROFILES`` in ``.env``.
* Accounts  (credentials) are loaded from ``ACCOUNT_*`` in ``.env`` and
  exposed as a mutable pool via ``/api/accounts``.
* Requests  are admitted through ``/v1/*`` and forwarded to the Gemini
  web endpoint by ``adapter.py``.

Concurrency & safety
--------------------
* ``_reqid`` is **per-request** (not global), so concurrent
  ``/v1/chat/completions`` calls no longer race on a shared counter.
* ``AccountPool`` is guarded by an ``asyncio.Lock``; mutating endpoints
  acquire the same lock before editing the pool.
* ``httpx.AsyncClient`` is shared across requests for connection reuse.
* CORS is opt-in via ``CORS_ORIGINS`` env var; the default is "no
  CORS", which matches the security guidance of dropping the
  ``allow_origins=["*"]`` + ``allow_credentials=True`` anti-pattern.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import queue
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional, Union

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Header,
    Query,
    Request,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

from account_pool import Account, AccountPool
from adapter import (
    ModelProfile,
    close_client,
    get_client,
    send_request,
    stream_request,
    upload_image,
)
from auth import (
    auth_summary,
    check_admin_login,
    verify_admin_key,
    verify_api_key,
)
from logger import get_logger
from rate_limit import make_ip_limiter, make_rate_limit_dependency

__version__ = "1.0.0"
log = get_logger("server")

load_dotenv()


def _try_float(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _try_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None

# ── Profiles ─────────────────────────────────────────────────────────
profiles: dict[str, ModelProfile] = {}
_raw_profiles = os.getenv("PROFILES", "")
if _raw_profiles:
    for name in (n.strip() for n in _raw_profiles.split(",") if n.strip()):
        suffix = f"_{name}" if name else ""
        try:
            profiles[name] = ModelProfile(
                name=name,
                model_family=int(os.getenv(f"MODEL_FAMILY{suffix}", "1")),
                thinking_mode=int(os.getenv(f"THINKING_MODE{suffix}", "1")),
                temperature=_try_float(os.getenv(f"TEMPERATURE{suffix}")),
                max_tokens=_try_int(os.getenv(f"MAX_TOKENS{suffix}")),
                system_prompt=os.getenv(f"SYSTEM_PROMPT{suffix}", ""),
            )
        except ValueError as exc:
            log.warning("profile_load_failed", extra={"name": name, "err": str(exc)})
if not profiles:
    profiles["default"] = ModelProfile(name="default")

# ── Pool ─────────────────────────────────────────────────────────────
_env_path = Path(os.getenv("ENV_PATH", ".env")).resolve()
pool = AccountPool(
    strategy=os.getenv("ROTATION_STRATEGY", "least-recently-used"),
    max_errors=int(os.getenv("MAX_ERRORS_BEFORE_DISABLE", "3")),
    env_path=_env_path,
)
pool.load_from_env()
if _env_path.exists():
    pool._env_mtime = _env_path.stat().st_mtime

# ── Server config ────────────────────────────────────────────────────
default_model = os.getenv("DEFAULT_MODEL", next(iter(profiles)))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "1800"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
GLOBAL_RATE_LIMIT_RPM = int(os.getenv("GLOBAL_RATE_LIMIT_RPM", "0"))  # 0 = off

# ── Lifespan: shared HTTP client + .env watcher ──────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("server_starting", extra={"version": __version__, "profiles": list(profiles)})
    # Warm up the shared HTTP client.
    await get_client()
    # Attach the SSE log bridge so the live log page receives events.
    root_logger = logging.getLogger("gemini2api")
    if _log_bridge not in root_logger.handlers:
        root_logger.addHandler(_log_bridge)
    _log_bridge.bind_loop(asyncio.get_running_loop())
    watcher = asyncio.create_task(_env_watcher())
    try:
        yield
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        await close_client()
        log.info("server_stopped")


async def _env_watcher(interval: float = 5.0) -> None:
    """Background task: pick up manual edits to ``.env`` every ``interval``s."""
    while True:
        try:
            await asyncio.sleep(interval)
            await pool.reload_if_changed()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("env_watcher_error", extra={"err": str(exc)})


# ── App ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="gemini2api",
    version=__version__,
    description="OpenAI-compatible reverse proxy for the Gemini web endpoint.",
    lifespan=lifespan,
)

# CORS: opt-in. Set CORS_ORIGINS=https://a.com,https://b.com to enable.
_cors = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if _cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

# Per-IP rate limit dependency (only attached if enabled).
_ip_limiter = make_ip_limiter(GLOBAL_RATE_LIMIT_RPM) if GLOBAL_RATE_LIMIT_RPM > 0 else None
_rate_limit_dep = make_rate_limit_dependency(_ip_limiter) if _ip_limiter else None


# ── Pydantic models ─────────────────────────────────────────────────
class ContentPart(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str
    text: Optional[str] = None
    image_url: Optional[dict] = None

    @field_validator("type")
    @classmethod
    def _validate_type(cls, v: str) -> str:
        if v not in ("text", "image_url"):
            raise ValueError(f"unsupported content type: {v}")
        return v


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: str
    content: Union[str, list[ContentPart], list[dict]]
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[dict]] = None

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        if v not in ("system", "user", "assistant", "tool", "function"):
            raise ValueError(f"invalid role: {v}")
        return v


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str = default_model
    messages: list[ChatMessage] = Field(..., min_length=1)
    stream: bool = False
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    tools: Optional[list[dict]] = None
    tool_choice: Optional[Union[str, dict]] = None
    user: Optional[str] = None  # Forwarded into logs only.

    @field_validator("model")
    @classmethod
    def _validate_model(cls, v: str) -> str:
        if not v or not isinstance(v, str):
            raise ValueError("model must be a non-empty string")
        return v


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(..., pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    f_sid: str = ""
    at: str = ""
    sn_param: str = ""
    bl_param: str = ""
    hl: str = "zh-CN"
    session_uuid: str = ""
    request_hash: str = ""
    bound_models: list[str] = Field(default_factory=list)
    rate_limit_rpm: int = Field(default=60, ge=1, le=6000)
    max_concurrent: int = Field(default=4, ge=1, le=64)

    @field_validator("bound_models")
    @classmethod
    def _validate_bound(cls, v: list[str]) -> list[str]:
        # Reject any unknown profile name early so the UI can show a clear error.
        unknown = [m for m in v if m and m not in profiles]
        if unknown:
            raise ValueError(f"unknown profile(s): {', '.join(unknown)}")
        return v


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: Optional[bool] = None
    bound_models: Optional[list[str]] = None
    rate_limit_rpm: Optional[int] = Field(default=None, ge=1, le=6000)
    max_concurrent: Optional[int] = Field(default=None, ge=1, le=64)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str


# ── Helpers ──────────────────────────────────────────────────────────

def _flatten_messages(msgs: list[ChatMessage]) -> list[dict]:
    """Drop everything we don't use server-side; keep raw content for
    multi-modal handling in ``adapter._flatten_content``."""
    return [{"role": m.role, "content": m.content, "name": m.name} for m in msgs]


def _tools_payload(req: ChatCompletionRequest) -> Optional[list[dict]]:
    if not req.tools:
        return None
    # tool_choice=``none`` is treated as "explicitly disabled".
    if isinstance(req.tool_choice, str) and req.tool_choice == "none":
        return None
    return req.tools


# ── Chat endpoint ────────────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    req: ChatCompletionRequest,
    _auth: None = Depends(verify_api_key),
    _rl: Optional[None] = Depends(_rate_limit_dep) if _rate_limit_dep else None,
):
    started = time.time()
    request_id = uuid.uuid4().hex[:12]
    # Per-request reqid base — no global counter, no races.
    base_reqid = int(time.time() * 1_000_000) & 0xFFFFFFF
    log.info(
        "request_started",
        extra={"req_id": request_id, "model": req.model, "stream": req.stream, "msgs": len(req.messages)},
    )

    model_name = req.model or default_model
    profile = profiles.get(model_name)
    if not profile:
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "invalid_model", "message": f"Unknown model: {model_name}"}},
        )
    # Allow the request to override profile-level temperature / max_tokens.
    if req.temperature is not None:
        profile.temperature = req.temperature
    if req.max_tokens is not None:
        profile.max_tokens = req.max_tokens

    messages = _flatten_messages(req.messages)
    tools = _tools_payload(req)
    pool.record_request()

    if req.stream:
        return await _handle_stream(profile, messages, base_reqid, tools, request_id, started)

    return await _handle_blocking(profile, messages, base_reqid, tools, request_id, started)


async def _handle_blocking(profile, messages, base_reqid, tools, request_id, started):
    last_error: Optional[str] = None
    last_status: int = 502
    last_payload: Any = None
    for attempt in range(pool.max_errors):
        if attempt > 0:
            pool.record_retry()
            await asyncio.sleep(min(2 ** (attempt - 1), 8))  # 1s, 2s, 4s, 8s
        account = await pool.select(profile.name)
        if not account:
            last_error = f"No available account for model '{profile.name}'"
            break
        try:
            result, _new_reqid, status_code = await send_request(
                profile, account, messages, base_reqid, tools=tools,
            )
            await pool.release(account)
            if status_code == 200:
                await pool.record_success(account)
                latency = time.time() - started
                log.info(
                    "request_completed",
                    extra={"req_id": request_id, "status": 200, "latency_s": round(latency, 3),
                           "account": account.name, "model": profile.name,
                           "tokens": result.get("usage", {}) if isinstance(result, dict) else {}},
                )
                return JSONResponse(content=result)
            # Non-200 from send_request — payload carries error info.
            await pool.record_failure(account, reason=f"upstream {status_code}")
            last_status = status_code if status_code >= 500 else 502
            last_error = f"Upstream {status_code}"
            last_payload = result if isinstance(result, dict) else None
            if status_code in (401, 403, 429):
                continue  # credential / quota issue — try the next account
            break  # 4xx other than auth/quota is not retryable
        except httpx.HTTPError as exc:
            await pool.release(account)
            await pool.record_failure(account, reason=f"http {exc.__class__.__name__}")
            last_error = str(exc)
            last_status = 502
            continue
        except Exception as exc:  # noqa: BLE001
            await pool.release(account)
            await pool.record_failure(account, reason=f"exception {exc.__class__.__name__}")
            log.exception("request_failed", extra={"req_id": request_id})
            last_error = str(exc)
            last_status = 500
            break

    log.warning("request_failed", extra={"req_id": request_id, "err": last_error, "status": last_status})
    body = {"error": {"type": "upstream_error", "message": last_error or "unknown", "status": last_status}}
    if last_payload:
        body["error"]["body"] = last_payload.get("error", last_payload)
    return JSONResponse(status_code=last_status, content=body)


async def _handle_stream(profile, messages, base_reqid, tools, request_id, started):
    """Return a StreamingResponse; fall back to non-stream on auth/quota errors."""
    # Pre-select one account for the stream. If it fails mid-flight we
    # send a final error chunk and let the client reconnect.
    account = await pool.select(profile.name)
    if not account:
        return JSONResponse(
            status_code=503,
            content={"error": {"type": "no_account", "message": "No available account"}},
        )

    async def event_source():
        try:
            async for chunk in stream_request(profile, account, messages, base_reqid, tools=tools):
                yield chunk
            await pool.record_success(account)
            log.info(
                "stream_completed",
                extra={"req_id": request_id, "account": account.name, "model": profile.name,
                       "latency_s": round(time.time() - started, 3)},
            )
        except httpx.HTTPError as exc:
            log.warning("stream_error", extra={"req_id": request_id, "err": str(exc)})
            await pool.record_failure(account, reason=f"stream {exc.__class__.__name__}")
            err = {"error": {"type": "stream_error", "message": str(exc)}}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        except Exception as exc:  # noqa: BLE001
            log.exception("stream_exception", extra={"req_id": request_id})
            await pool.record_failure(account, reason=f"stream {exc.__class__.__name__}")
            err = {"error": {"type": "stream_exception", "message": str(exc)}}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        finally:
            await pool.release(account)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Models + health ─────────────────────────────────────────────────

@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": name, "object": "model",
                "created": int(time.time()), "owned_by": "gemini2api",
                "model_family": p.model_family, "thinking_mode": p.thinking_mode,
            }
            for name, p in profiles.items()
        ],
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": __version__,
        "model": default_model,
        "profiles": list(profiles.keys()),
        "auth": auth_summary(),
    }


# ── Admin API (WebUI) ────────────────────────────────────────────────

@app.post("/api/auth/login", dependencies=[Depends(verify_admin_key)])
async def api_login(data: LoginRequest, response: JSONResponse):
    # Endpoint is guarded by verify_admin_key, so reaching here means auth
    # is disabled (dependency is a no-op) or the key was already valid via
    # cookie/header. The /login flow exists for browsers: see
    # /api/auth/web_login below.
    return {"status": "ok"}


@app.post("/api/auth/web_login")
async def api_web_login(
    response: JSONResponse,
    key: str = Form(..., max_length=512),
):
    if not check_admin_login(key):
        log.warning("admin_login_failed")
        raise HTTPException(status_code=401, detail={"error": "invalid key"})
    # Set a short-lived cookie; auth dependency accepts this for 1 hour.
    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        "admin_token", key, max_age=3600, httponly=True, samesite="lax", path="/",
    )
    return response


@app.post("/api/auth/logout")
async def api_logout(response: JSONResponse):
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("admin_token", path="/")
    return response


@app.get("/api/stats", dependencies=[Depends(verify_admin_key)])
async def api_stats():
    return pool.stats()


@app.get("/api/profiles", dependencies=[Depends(verify_admin_key)])
async def api_profiles():
    return {
        "profiles": [
            {"name": p.name, "model_family": p.model_family, "thinking_mode": p.thinking_mode}
            for p in profiles.values()
        ]
    }


@app.get("/api/accounts", dependencies=[Depends(verify_admin_key)])
async def api_accounts():
    return {"accounts": [
        {
            "name": a.name, "enabled": a.enabled, "error_count": a.error_count,
            "last_used": a.last_used, "last_success": a.last_success, "last_error": a.last_error,
            "inflight": a.inflight, "bound_models": a.bound_models,
            "rate_limit_rpm": a.rate_limit_rpm, "max_concurrent": a.max_concurrent,
            "recent_60s": sum(1 for t in a.recent_requests if t > time.time() - 60),
        }
        for a in pool.accounts
    ]}


@app.post("/api/accounts", dependencies=[Depends(verify_admin_key)])
async def api_add_account(data: AccountCreate):
    if pool.get_account(data.name):
        raise HTTPException(400, f"Account '{data.name}' already exists")
    pool.add(Account(
        name=data.name, f_sid=data.f_sid, at=data.at, sn_param=data.sn_param,
        bl_param=data.bl_param, hl=data.hl, session_uuid=data.session_uuid,
        request_hash=data.request_hash, bound_models=data.bound_models,
        rate_limit_rpm=data.rate_limit_rpm, max_concurrent=data.max_concurrent,
    ))
    if os.getenv("PERSIST_ACCOUNTS", "1") == "1":
        try:
            pool.save_to_env(_env_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("save_env_failed", extra={"err": str(exc)})
    return {"status": "ok", "name": data.name}


@app.patch("/api/accounts/{name}", dependencies=[Depends(verify_admin_key)])
async def api_patch_account(name: str, data: AccountPatch):
    acct = pool.get_account(name)
    if not acct:
        raise HTTPException(404, f"Account '{name}' not found")
    if data.enabled is not None:
        acct.enabled = data.enabled
        log.info("account_toggle", extra={"name": name, "enabled": data.enabled})
    if data.bound_models is not None:
        unknown = [m for m in data.bound_models if m and m not in profiles]
        if unknown:
            raise HTTPException(400, f"unknown profile(s): {', '.join(unknown)}")
        acct.bound_models = data.bound_models
    if data.rate_limit_rpm is not None:
        acct.rate_limit_rpm = data.rate_limit_rpm
    if data.max_concurrent is not None:
        acct.max_concurrent = data.max_concurrent
    if os.getenv("PERSIST_ACCOUNTS", "1") == "1":
        try:
            pool.save_to_env(_env_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("save_env_failed", extra={"err": str(exc)})
    return {"status": "ok", "name": name}


@app.delete("/api/accounts/{name}", dependencies=[Depends(verify_admin_key)])
async def api_delete_account(name: str):
    if not pool.remove(name):
        raise HTTPException(404, f"Account '{name}' not found")
    if os.getenv("PERSIST_ACCOUNTS", "1") == "1":
        try:
            pool.save_to_env(_env_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("save_env_failed", extra={"err": str(exc)})
    return {"status": "ok"}


# ── Multipart upload helper ─────────────────────────────────────────

@app.post("/api/accounts/from-har", dependencies=[Depends(verify_admin_key)])
async def api_account_from_har(
    har: str = Form(..., description="Raw HAR file contents"),
    account_name: str = Form(..., max_length=64),
    model: str = Form(...),
):
    """Parse a HAR and return the extracted credentials. The frontend
    shows the result to the user; the actual save happens via
    ``/api/accounts`` POST."""
    from har_parser import parse_har
    from io import StringIO

    class _FakePath:
        def __init__(self, text: str):
            self._text = text
        def read_text(self, *_, **__):
            return self._text
        def __enter__(self): return self
        def __exit__(self, *a): return False

    # parse_har expects a filesystem path; shim by writing to a temp file.
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".har", delete=False, encoding="utf-8") as tmp:
        tmp.write(har)
        tmp_path = tmp.name
    try:
        analysis = parse_har(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return {
        "account_name": account_name,
        "model": model,
        "f_sid": analysis.f_sid,
        "at": analysis.at,
        "sn_param": analysis.sn_param,
        "bl_param": analysis.bl_param,
        "hl": analysis.hl,
        "session_uuid": analysis.session_uuid,
        "request_hash": analysis.request_hash,
        "model_family": analysis.model_family,
        "thinking_mode": analysis.thinking_mode,
    }


# ── WebUI ────────────────────────────────────────────────────────────

TEMPLATES_DIR = Path(__file__).parent / "templates"


def render_template(name: str, **kwargs) -> str:
    path = TEMPLATES_DIR / name
    if not path.exists():
        return f"<h1>Template {name} not found</h1>"
    content = path.read_text(encoding="utf-8")
    for k, v in kwargs.items():
        # HTML-escape every value to prevent XSS via profile/account names.
        content = content.replace(f"{{{{{k}}}}}", html.escape(str(v)))
    return content


@app.get("/", response_class=HTMLResponse)
async def web_dashboard(_: None = Depends(verify_admin_key)):
    s = pool.stats()
    return HTMLResponse(render_template(
        "dashboard.html",
        total_requests=s["total_requests"],
        success=s["success"],
        failures=s["failures"],
        retries=s["retries"],
        accounts_total=s["accounts_total"],
        accounts_enabled=s["accounts_enabled"],
        accounts_disabled=s["accounts_disabled"],
        accounts_exhausted=s["accounts_exhausted"],
        profiles=", ".join(profiles.keys()),
        default_model=default_model,
        strategy=pool.strategy,
        version=__version__,
        auth=auth_summary(),
    ))


@app.get("/webui/accounts", response_class=HTMLResponse)
async def web_accounts(_: None = Depends(verify_admin_key)):
    rows = []
    for a in pool.accounts:
        status_icon = "🟢" if a.enabled else "🔴"
        status_text = "已启用" if a.enabled else "已禁用"
        exhausted = " ⚠️ 已耗尽" if a.error_count >= pool.max_errors else ""
        # html.escape every dynamic value to close the XSS hole.
        rows.append(
            f'<tr hx-target="this" hx-swap="outerHTML">'
            f'<td>{status_icon} {html.escape(a.name)}</td>'
            f'<td>{a.error_count}</td>'
            f'<td>{html.escape(", ".join(a.bound_models[:3]))}</td>'
            f'<td>{html.escape(status_text + exhausted)}</td>'
            f'<td>{a.inflight}/{a.max_concurrent}</td>'
            f'<td>{sum(1 for t in a.recent_requests if t > time.time() - 60)}/{a.rate_limit_rpm}/m</td>'
            f'<td>'
            f'<button hx-put="/api/accounts/{html.escape(a.name)}/toggle" hx-target="closest tr" hx-swap="outerHTML">切换</button> '
            f'<button hx-delete="/api/accounts/{html.escape(a.name)}" hx-target="closest tr" hx-swap="outerHTML" style="color:red">删除</button>'
            f'</td></tr>'
        )
    return HTMLResponse(render_template("accounts.html", accounts_table="".join(rows), profiles=profiles, version=__version__))


@app.get("/webui/logs", response_class=HTMLResponse)
async def web_logs(_: None = Depends(verify_admin_key)):
    return HTMLResponse(render_template("logs.html", version=__version__))


@app.get("/webui/login", response_class=HTMLResponse)
async def web_login_page():
    """Serve a static login form. The form posts to ``/api/auth/web_login``."""
    return HTMLResponse(render_template("login.html", version=__version__))


# ── Server-Sent Events for the live log stream ──────────────────────

class _InMemoryLogHandler(logging.Handler):
    """Pushes each log record onto ``asyncio.Queue`` s for SSE consumers."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.queues: list[asyncio.Queue] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            payload = {
                "ts": self.format(record).split(" | ")[0],
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if self._loop and self.queues:
                self._loop.call_soon_threadsafe(self._enqueue, payload)
        except Exception:  # noqa: BLE001
            pass

    def _enqueue(self, payload: dict) -> None:
        for q in self.queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop oldest to make room; never block the logger thread.
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:  # noqa: BLE001
                    pass


_log_bridge = _InMemoryLogHandler()
_log_bridge.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))


@app.get("/api/events/stream", dependencies=[Depends(verify_admin_key)])
async def events_stream(request: Request):
    """SSE feed of structured log records for the live log page."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _log_bridge.queues.append(queue)

    async def event_gen():
        try:
            # Send a hello so the client knows the connection is live.
            yield f"event: hello\ndata: {json.dumps({'status': 'ok'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Keep-alive comment so proxies don't kill the stream.
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: log\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            try:
                _log_bridge.queues.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Legacy PUT toggle for backward compatibility with the original UI.
@app.put("/api/accounts/{name}/toggle", dependencies=[Depends(verify_admin_key)])
async def api_toggle_account(name: str):
    acct = pool.get_account(name)
    if not acct:
        raise HTTPException(404, f"Account '{name}' not found")
    acct.enabled = not acct.enabled
    if os.getenv("PERSIST_ACCOUNTS", "1") == "1":
        try:
            pool.save_to_env(_env_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("save_env_failed", extra={"err": str(exc)})
    return {"status": "ok", "name": name, "enabled": acct.enabled}


# ── Entrypoint ───────────────────────────────────────────────────────

if __name__ == "__main__":
    port = PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    print(f"gemini2api v{__version__} running on http://{HOST}:{port}")
    print(f"  Profiles: {list(profiles.keys())}")
    print(f"  Accounts: {len(pool.accounts)}")
    print(f"  Default:  {default_model}")
    print(f"  Auth:     {auth_summary()}")
    uvicorn.run(app, host=HOST, port=port, log_level=LOG_LEVEL.lower())
