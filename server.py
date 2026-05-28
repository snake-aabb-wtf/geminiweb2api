import os
import sys
import json
import time
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, Union

load_dotenv()

from adapter import ChatAdapter, ModelProfile


def load_profiles() -> dict[str, ModelProfile]:
    profiles = {}
    model_names = []

    raw = os.getenv("MODEL_NAMES", "")
    if raw:
        model_names = [n.strip() for n in raw.split(",") if n.strip()]

    if not model_names:
        model_names = ["default"]

    for name in model_names:
        suffix = f"_{name}" if name != "default" else ""
        p = ModelProfile(
            name=name,
            model_family=int(os.getenv(f"MODEL_FAMILY{suffix}", "1")),
            thinking_mode=int(os.getenv(f"THINKING_MODE{suffix}", "1")),
            f_sid=os.getenv(f"F_SID{suffix}", ""),
            at=os.getenv(f"AT{suffix}", ""),
            sn_param=os.getenv(f"SN_PARAM{suffix}", ""),
            bl_param=os.getenv(f"BL_PARAM{suffix}", ""),
            hl=os.getenv(f"HL{suffix}", "zh-CN"),
            session_uuid=os.getenv(f"UUID{suffix}", ""),
            request_hash=os.getenv(f"HASH{suffix}", ""),
        )
        for key, val in os.environ.items():
            if key.lower().startswith("header_") and val:
                hname = key[7:]
                p.headers[hname] = val
        profiles[name] = p

    return profiles


profiles = load_profiles()
default_model = os.getenv("DEFAULT_MODEL", "default")
if default_model not in profiles and profiles:
    default_model = next(iter(profiles))

adapter = ChatAdapter(profiles=profiles, default_model=default_model)

MODEL_NAME = os.getenv("MODEL_NAME", default_model)
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "1800"))
API_KEY = os.getenv("API_KEY", "sk-web2api-placeholder")

app = FastAPI(title="gemini2api", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "gemini2api",
            }
            for name in adapter.profiles
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    if request.model:
        adapter.set_model(request.model)

    messages = [{"role": m.role, "content": m.content} for m in request.messages]
    stream = request.stream
    payload = adapter.convert_request(messages, stream=stream)

    if stream:
        return StreamingResponse(
            adapter.stream_request(payload),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        try:
            response = await adapter.send_request(payload)
            return response
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[ERROR] {tb}", flush=True)
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": f"Gemini upstream error: {str(e)}",
                        "type": "upstream_error",
                        "code": 502,
                        "detail": tb[-500:],
                    }
                },
            )


@app.get("/health")
async def health():
    return {"status": "ok", "model": adapter.model_name, "profiles": list(adapter.profiles.keys())}


if __name__ == "__main__":
    port = PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    print(f"gemini2api proxy running on http://{HOST}:{port}")
    print(f"  Profiles: {list(profiles.keys())}")
    print(f"  Default:  {default_model}")
    print(f"\n  Test with:")
    print(f'    curl http://localhost:{port}/v1/chat/completions \\')
    print(f'      -H "Content-Type: application/json" \\')
    print(f'      -d \'{{"model":"{default_model}","messages":[{{"role":"user","content":"Hello"}}]}}\'')
    uvicorn.run(app, host=HOST, port=port)
