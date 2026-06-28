"""FastAPI entrypoint for gemini2api v1.1.

Layout
------
* Profiles  (model definitions) are loaded from ``PROFILES`` in ``.env``.
* Accounts  (credentials) are loaded from ``ACCOUNT_*`` in ``.env`` and
  exposed as a mutable pool via ``/api/accounts``.
* Requests  are admitted through ``/v1/*`` and forwarded to the Gemini
  web endpoint by ``adapter.py``.

v1.1 changes
-------------
* ``CHAT_MAX_RETRIES`` decoupled from ``MAX_ERRORS_BEFORE_DISABLE``.
* Per-request temperature / max_tokens no longer mutate the shared
  ``ModelProfile`` (we shallow-copy with ``dataclasses.replace``).
* Streaming responses retry across accounts until the *first* chunk
  is delivered — once bytes hit the wire, the OpenAI protocol forbids
  swapping mid-stream.
* Server actually wires ``adapter.upload_image`` so user-supplied
  ``image_url`` content reaches Gemini instead of being dropped.
* New endpoints: ``/v1/embeddings`` (stub), ``/api/usage``,
  ``/api/health/accounts``, and the SSE queue is garbage-collected
  against disconnected clients.
"""
from __future__ import annotations

import asyncio
import dataclasses
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
)
import adapter  # imported as module so test can patch adapter.upload_image
from auth import (
    auth_summary,
    check_admin_login,
    verify_admin_key,
    verify_api_key,
)
from logger import get_logger, scrub_pii
from rate_limit import make_ip_limiter, make_rate_limit_dependency

__version__ = "1.1.0"
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
CHAT_MAX_RETRIES = max(1, int(os.getenv("CHAT_MAX_RETRIES", "2")))
EMBEDDINGS_ENABLED = os.getenv("GEMINI_EMBEDDINGS_ENABLED", "0") == "1"
EMBEDDINGS_DIM = int(os.getenv("GEMINI_EMBEDDINGS_DIM", "768"))
LOG_SCRUB_PII = os.getenv("LOG_SCRUB_PII", "0") == "1"
HEALTH_CHECK_TIMEOUT = float(os.getenv("HEALTH_CHECK_TIMEOUT", "30"))

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


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str = default_model
    input: Union[str, list[str]]
    user: Optional[str] = None
    encoding_format: Optional[str] = "float"


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


# ── Helpers ──────────────────────────────────────────────────────────

def _collect_image_urls(msgs: list[ChatMessage]) -> list[str]:
    """Pull every ``image_url.url`` out of an OpenAI-style message list.

    We do this here rather than in ``adapter._flatten_content`` because
    uploading each image is an async I/O step that must run inside the
    server's event loop with the right HTTP client.

    Pydantic v2 will have already coerced the input JSON into
    ``ContentPart`` model instances (or kept raw ``dict`` s if the
    consumer chose that branch), so we accept both shapes.
    """
    out: list[str] = []
    for m in msgs:
        content = m.content
        if not isinstance(content, list):
            continue
        for part in content:
            # ``ContentPart`` is a BaseModel — expose the fields via
            # ``__dict__`` so the same code path handles both Pydantic
            # models and raw dicts.
            if hasattr(part, "__dict__") and not isinstance(part, dict):
                part = part.__dict__
            if not isinstance(part, dict):
                continue
            if part.get("type") != "image_url":
                continue
            url = (part.get("image_url") or {}).get("url")
            if url:
                out.append(url)
    return out


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
    # Optionally redact PII from the preview we log. The full message
    # content never enters the log; we only emit a short head + tail.
    if LOG_SCRUB_PII and req.messages:
        head = str(req.messages[0].content)[:120]
        head = scrub_pii(head)
    else:
        head = ""
    log.info(
        "request_started",
        extra={"req_id": request_id, "model": req.model, "stream": req.stream,
               "msgs": len(req.messages), "head": head if LOG_SCRUB_PII else ""},
    )

    model_name = req.model or default_model
    profile = profiles.get(model_name)
    if not profile:
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "invalid_model", "message": f"Unknown model: {model_name}"}},
        )
    # Shallow-copy the profile so per-request overrides (temperature,
    # max_tokens) don't race against other concurrent requests that
    # share the same profile object.
    if req.temperature is not None or req.max_tokens is not None:
        profile = dataclasses.replace(
            profile,
            temperature=req.temperature if req.temperature is not None else profile.temperature,
            max_tokens=req.max_tokens if req.max_tokens is not None else profile.max_tokens,
        )

    messages = _flatten_messages(req.messages)
    tools = _tools_payload(req)
    pool.record_request()

    # If the request carries image_url content, upload each one to Gemini
    # so the upstream model actually sees them. Failures are logged but
    # not fatal — we still forward the text part.
    image_urls = _collect_image_urls(req.messages)
    attachments: list[dict] = []
    if image_urls:
        # Pre-select an account for upload (no rotation here: uploads
        # are tiny and the pool will hand out a different one for the
        # main request if needed).
        upload_account = await pool.select(profile.name)
        if upload_account is not None:
            try:
                for url in image_urls:
                    upload_id = await adapter.upload_image(upload_account, url)
                    if upload_id:
                        attachments.append({"type": "upload_id", "value": upload_id})
                    else:
                        log.warning(
                            "upload_skipped",
                            extra={"req_id": request_id, "url_prefix": url[:60]},
                        )
            except Exception as exc:  # noqa: BLE001
                log.warning("upload_failed", extra={"req_id": request_id, "err": str(exc)})
            finally:
                await pool.release(upload_account)

    if req.stream:
        return await _handle_stream(
            profile, messages, base_reqid, tools, request_id, started,
            attachments=attachments,
        )

    return await _handle_blocking(
        profile, messages, base_reqid, tools, request_id, started,
        attachments=attachments,
    )


async def _handle_blocking(
    profile, messages, base_reqid, tools, request_id, started,
    attachments: Optional[list[dict]] = None,
):
    last_error: Optional[str] = None
    last_status: int = 502
    last_payload: Any = None
    for attempt in range(CHAT_MAX_RETRIES):
        if attempt > 0:
            pool.record_retry()
            await asyncio.sleep(min(2 ** (attempt - 1), 8))  # 1s, 2s, 4s, 8s
        account = await pool.select(profile.name)
        if not account:
            last_error = f"No available account for model '{profile.name}'"
            break
        try:
            result, _new_reqid, status_code = await adapter.send_request(
                profile, account, messages, base_reqid, tools=tools,
                attachments=attachments,
            )
            await pool.release(account)
            if status_code == 200:
                await pool.record_success(account)
                # Feed the rolling usage log.
                usage = result.get("usage") if isinstance(result, dict) else None
                if isinstance(usage, dict):
                    await pool.record_usage(
                        account,
                        profile.name,
                        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                        completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                        total_tokens=int(usage.get("total_tokens", 0) or 0),
                    )
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


async def _handle_stream(
    profile, messages, base_reqid, tools, request_id, started,
    attachments: Optional[list[dict]] = None,
):
    """Return a StreamingResponse; retry across accounts **before** the
    first byte hits the wire.

    The OpenAI streaming protocol binds the entire stream to a single
    assistant message id, so we cannot swap accounts after a chunk has
    been delivered. The retry loop therefore re-selects an account on
    *connection* failure (before yielding the first chunk) and then
    commits to that account for the lifetime of the stream.
    """
    last_error: Optional[str] = None
    chosen_account = None

    for attempt in range(CHAT_MAX_RETRIES):
        if attempt > 0:
            pool.record_retry()
            await asyncio.sleep(min(2 ** (attempt - 1), 4))  # 1s, 2s, 4s
        account = await pool.select(profile.name)
        if not account:
            last_error = f"No available account for model '{profile.name}'"
            break
        # Quick probe: try to open the stream and read the first chunk.
        # If that succeeds we commit; if it raises we release + retry.
        try:
            gen = adapter.stream_request(profile, account, messages, base_reqid, tools=tools, attachments=attachments)
            first_chunk = await gen.__anext__()
        except StopAsyncIteration:
            # Empty stream — treat as success, nothing more to yield.
            await pool.release(account)
            await pool.record_success(account)
            return StreamingResponse(
                iter([b"data: [DONE]\n\n"]),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001
            await pool.release(account)
            await pool.record_failure(account, reason=f"stream_setup {exc.__class__.__name__}")
            last_error = str(exc)
            log.warning("stream_setup_failed", extra={"req_id": request_id, "attempt": attempt, "err": str(exc)})
            continue
        # Committed. Build the live stream.
        chosen_account = account
        break

    if chosen_account is None:
        return JSONResponse(
            status_code=502,
            content={"error": {"type": "upstream_error", "message": last_error or "stream setup failed"}},
        )

    account = chosen_account

    async def event_source():
        try:
            # Re-yield the first chunk we already pulled, then drain the
            # rest of the generator. We can't reuse the same generator
            # object (it's already been advanced), so wrap it.
            yield first_chunk
            async for chunk in gen:
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


@app.post("/v1/embeddings", dependencies=[Depends(verify_api_key)])
async def create_embeddings(req: EmbeddingRequest):
    """Stub embeddings endpoint.

    The Gemini Web endpoint does not expose a stable embeddings
    surface, so we return deterministic zero vectors. The
    ``X-Gemini2api-Status: stub`` header makes the situation obvious
    to clients (and avoids burning a Google account on each call).

    Enable explicitly with ``GEMINI_EMBEDDINGS_ENABLED=1``.
    """
    if not EMBEDDINGS_ENABLED:
        return JSONResponse(
            status_code=501,
            content={"error": {"type": "embeddings_disabled",
                               "message": "Set GEMINI_EMBEDDINGS_ENABLED=1 to enable"}},
        )
    inputs = req.input if isinstance(req.input, list) else [req.input]
    dim = EMBEDDINGS_DIM
    data = [
        {
            "object": "embedding",
            "index": i,
            "embedding": [0.0] * dim,
        }
        for i, _ in enumerate(inputs)
    ]
    return JSONResponse(
        content={
            "object": "list",
            "model": req.model,
            "data": data,
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        },
        headers={"X-Gemini2api-Status": "stub"},
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": __version__,
        "model": default_model,
        "profiles": list(profiles.keys()),
        "auth": auth_summary(),
        "embeddings_enabled": EMBEDDINGS_ENABLED,
        "log_scrub_pii": LOG_SCRUB_PII,
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
    return await pool.stats()


@app.get("/api/usage", dependencies=[Depends(verify_admin_key)])
async def api_usage(hours: int = Query(24, ge=1, le=168)):
    """Return minute-bucketed token usage for the last ``hours`` hours.

    Default is 24h. Max is 168h (7 days) to keep payloads bounded.
    """
    series = pool.usage_series(hours=hours)
    # Aggregate a small summary for the dashboard header.
    total_prompt = sum(t["prompt"] for t in series)
    total_completion = sum(t["completion"] for t in series)
    total_requests = sum(t["success_count"] for t in series)
    return {
        "hours": hours,
        "summary": {
            "prompt": total_prompt,
            "completion": total_completion,
            "total": total_prompt + total_completion,
            "requests": total_requests,
        },
        "series": series,
    }


@app.post("/api/health/accounts", dependencies=[Depends(verify_admin_key)])
async def api_health_accounts():
    """Probe every enabled account with a minimal request and report status.

    Concurrently calls each account's bound models with a ``"ping"``
    message and ``max_tokens=1``. The result is per-account:
    ``{name, ok, status_code, latency_ms, error}``. Failures are *not*
    persisted to the pool — this is a read-only health check.
    """
    ping_messages = [{"role": "user", "content": "ping"}]
    targets: list[tuple] = []
    for a in pool.accounts:
        if not a.enabled:
            continue
        for model_name in a.bound_models:
            profile = profiles.get(model_name)
            if not profile:
                continue
            targets.append((a, profile, model_name))

    async def probe_one(acct, prof, model_name):
        started = time.time()
        try:
            _result, _reqid, status_code = await asyncio.wait_for(
                adapter.send_request(
                    prof,
                    acct,
                    ping_messages,
                    int(time.time() * 1_000_000) & 0xFFFFFFF,
                ),
                timeout=globals().get("HEALTH_CHECK_TIMEOUT", 30),
            )
            ok = status_code == 200
            err = None
        except asyncio.TimeoutError:
            status_code = 0
            ok = False
            err = "timeout"
        except Exception as exc:  # noqa: BLE001
            status_code = 0
            ok = False
            err = str(exc)[:200]
        return {
            "name": acct.name,
            "model": model_name,
            "ok": ok,
            "status_code": status_code,
            "latency_ms": int((time.time() - started) * 1000),
            "error": err,
        }

    results = await asyncio.gather(*(probe_one(a, p, m) for a, p, m in targets))
    return {"checked_at": time.time(), "results": results}


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
    s = await pool.stats()
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
        # Track disconnected queues with the wall-clock time they were
        # marked dead so a background sweeper can remove them. We keep
        # them in the list for a short grace period to absorb transient
        # client reconnects.
        self._dead: dict = {}

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
        # Prune dead queues that have been waiting for GC long enough.
        now = time.time()
        if self._dead:
            stale = [q for q, ts in self._dead.items() if now - ts > 30]
            for q in stale:
                try:
                    self.queues.remove(q)
                    self._dead.pop(q, None)
                except ValueError:
                    pass
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

    def mark_dead(self, queue) -> None:
        """Flag a queue as dead. The next enqueue pass will remove it."""
        self._dead[queue] = time.time()

    def force_remove(self, queue) -> None:
        try:
            self.queues.remove(queue)
        except ValueError:
            pass
        self._dead.pop(id(queue), None)


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
            # Mark dead so the next enqueue sweep prunes us; if no
            # further events come through, force-remove immediately so
            # we never leak a queue reference.
            _log_bridge.mark_dead(queue)
            _log_bridge.force_remove(queue)

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
