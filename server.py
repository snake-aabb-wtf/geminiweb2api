import os
import sys
import json
import time
import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel
from typing import Optional, Union
from pathlib import Path

load_dotenv()

from adapter import ModelProfile, send_request, stream_request, convert_response
from account_pool import AccountPool

# ── Load profiles (model config only) ──────────────────────────────
profiles: dict[str, ModelProfile] = {}
raw = os.getenv("PROFILES", "")
if raw:
    for name in (n.strip() for n in raw.split(",") if n.strip()):
        suffix = f"_{name}" if name else ""
        profiles[name] = ModelProfile(
            name=name,
            model_family=int(os.getenv(f"MODEL_FAMILY{suffix}", "1")),
            thinking_mode=int(os.getenv(f"THINKING_MODE{suffix}", "1")),
        )

if not profiles:
    profiles["default"] = ModelProfile(name="default")

# ── Load account pool ──────────────────────────────────────────────
pool = AccountPool(
    strategy=os.getenv("ROTATION_STRATEGY", "least-recently-used"),
    max_errors=int(os.getenv("MAX_ERRORS_BEFORE_DISABLE", "3")),
)
pool.load_from_env()

# ── Server config ──────────────────────────────────────────────────
default_model = os.getenv("DEFAULT_MODEL", "default")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "1800"))
API_KEY = os.getenv("API_KEY", "sk-web2api-placeholder")
_reqid = int(time.time() * 1000) % 10000000

app = FastAPI(title="gemini2api", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic models ────────────────────────────────────────────────
class ContentPart(BaseModel):
    type: str
    text: Optional[str] = None

class ChatMessage(BaseModel):
    role: str
    content: Union[str, list[ContentPart]]

class ChatCompletionRequest(BaseModel):
    model: str = default_model
    messages: list[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


# ── Chat handler ───────────────────────────────────────────────────
def get_model_and_account(model_name: str):
    profile = profiles.get(model_name)
    if not profile:
        return None, None, f"Unknown model: {model_name}"
    account = pool.select(model_name)
    if not account:
        return None, None, f"No available account for model '{model_name}'"
    return profile, account, None


# ── OpenAI compatible endpoints ────────────────────────────────────
@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": int(time.time()), "owned_by": "gemini2api"}
            for name in profiles
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    global _reqid
    model_name = request.model or default_model
    profile, account, err = get_model_and_account(model_name)
    if err:
        return JSONResponse(status_code=400, content={"error": err})

    messages = [{"role": m.role, "content": m.content} for m in request.messages]
    stream = request.stream

    if stream:
        return StreamingResponse(
            stream_request(profile, account, messages, _reqid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # Non-streaming with rotation
    max_retries = pool.max_errors
    last_error = None
    for attempt in range(max_retries):
        if attempt > 0:
            pool.record_retry()
            account = pool.select(model_name)
            if not account:
                break
        try:
            result, new_reqid, status = await send_request(profile, account, messages, _reqid)
            _reqid = new_reqid
            pool.record_request()
            if status == 200:
                pool.record_success(account)
                return result
            pool.record_failure(account)
        except httpx.HTTPStatusError as e:
            pool.record_failure(account)
            last_error = f"Upstream {e.response.status_code}"
            if e.response.status_code in (401, 403):
                continue
            break
        except Exception as e:
            pool.record_failure(account)
            last_error = str(e)
            break

    return JSONResponse(status_code=502, content={"error": f"Gemini upstream error: {last_error}"})


@app.get("/health")
async def health():
    return {"status": "ok", "model": default_model, "profiles": list(profiles.keys())}


# ── Admin API ──────────────────────────────────────────────────────
from fastapi import HTTPException
from pydantic import BaseModel as PydanticBase

class AccountCreate(PydanticBase):
    name: str
    f_sid: str = ""
    at: str = ""
    sn_param: str = ""
    bl_param: str = ""
    hl: str = "zh-CN"
    session_uuid: str = ""
    request_hash: str = ""
    bound_models: list[str] = []


@app.get("/api/stats")
async def api_stats():
    return pool.stats()


@app.get("/api/accounts")
async def api_accounts():
    return {
        "accounts": [
            {
                "name": a.name,
                "enabled": a.enabled,
                "error_count": a.error_count,
                "last_used": a.last_used,
                "bound_models": a.bound_models,
            }
            for a in pool.accounts
        ]
    }


@app.post("/api/accounts")
async def api_add_account(data: AccountCreate):
    if pool.get_account(data.name):
        raise HTTPException(400, f"Account '{data.name}' already exists")
    from account_pool import Account
    acct = Account(
        name=data.name, f_sid=data.f_sid, at=data.at, sn_param=data.sn_param,
        bl_param=data.bl_param, hl=data.hl, session_uuid=data.session_uuid,
        request_hash=data.request_hash, bound_models=data.bound_models,
    )
    pool.add(acct)
    return {"status": "ok", "name": data.name}


@app.delete("/api/accounts/{name}")
async def api_delete_account(name: str):
    acct = pool.get_account(name)
    if not acct:
        raise HTTPException(404, f"Account '{name}' not found")
    pool.accounts.remove(acct)
    return {"status": "ok"}


@app.put("/api/accounts/{name}/toggle")
async def api_toggle_account(name: str):
    acct = pool.get_account(name)
    if not acct:
        raise HTTPException(404, f"Account '{name}' not found")
    acct.enabled = not acct.enabled
    return {"status": "ok", "name": name, "enabled": acct.enabled}


@app.get("/api/profiles")
async def api_profiles():
    return {"profiles": [{"name": p.name, "model_family": p.model_family, "thinking_mode": p.thinking_mode} for p in profiles.values()]}


# ── WebUI ──────────────────────────────────────────────────────────
TEMPLATES_DIR = Path(__file__).parent / "templates"


def render_template(name: str, **kwargs) -> str:
    path = TEMPLATES_DIR / name
    if not path.exists():
        return f"<h1>Template {name} not found</h1>"
    content = path.read_text(encoding="utf-8")
    for k, v in kwargs.items():
        content = content.replace(f"{{{{{k}}}}}", str(v))
    return content


@app.get("/", response_class=HTMLResponse)
async def web_dashboard():
    s = pool.stats()
    html = render_template("dashboard.html",
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
    )
    return HTMLResponse(html)


@app.get("/webui/accounts", response_class=HTMLResponse)
async def web_accounts():
    rows = ""
    for a in pool.accounts:
        status_icon = "🟢" if a.enabled else "🔴"
        status_text = "已启用" if a.enabled else "已禁用"
        exhausted = "⚠️ 已耗尽" if a.error_count >= pool.max_errors else ""
        rows += f"""
        <tr>
            <td>{status_icon} {a.name}</td>
            <td>{a.error_count}</td>
            <td>{', '.join(a.bound_models[:3])}</td>
            <td>{status_text} {exhausted}</td>
            <td>
                <button hx-put="/api/accounts/{a.name}/toggle" hx-target="closest tr" hx-swap="outerHTML">切换</button>
                <button hx-delete="/api/accounts/{a.name}" hx-target="closest tr" hx-swap="outerHTML" style="color:red">删除</button>
            </td>
        </tr>"""
    html = render_template("accounts.html", accounts_table=rows)
    return HTMLResponse(html)


if __name__ == "__main__":
    port = PORT
    if len(sys.argv) > 1:
        try: port = int(sys.argv[1])
        except ValueError: pass
    print(f"gemini2api v0.3.0 running on http://{HOST}:{port}")
    print(f"  Profiles: {list(profiles.keys())}")
    print(f"  Accounts: {len(pool.accounts)}")
    print(f"  Default:  {default_model}")
    uvicorn.run(app, host=HOST, port=port)
