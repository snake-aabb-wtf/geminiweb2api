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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HAR_PATH_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(SCRIPT_DIR)), "gemini.google.com.har")

load_dotenv()

from har_parser import parse_har
from adapter import ChatAdapter

HAR_PATH = os.getenv("HAR_PATH", HAR_PATH_DEFAULT)

if os.path.exists(HAR_PATH):
    analysis = parse_har(HAR_PATH)
    adapter = ChatAdapter(
        cookies=analysis.cookies,
        base_url=analysis.base_url,
    )
    adapter.set_har_params(analysis)
else:
    adapter = ChatAdapter(
        cookies=os.getenv("COOKIES", ""),
        base_url=os.getenv("TARGET_URL", "https://gemini.google.com"),
    )
    adapter.bl_param = os.getenv("BL_PARAM", adapter.bl_param)
    adapter.f_sid = os.getenv("F_SID", "")
    adapter.hl = os.getenv("HL", "zh-CN")
    adapter.at = os.getenv("AT", "")
    adapter._sn_param = os.getenv("SN_PARAM", "")
    adapter.cookies = os.getenv("COOKIES", "")

TARGET_URL = adapter.base_url
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "7897"))
API_KEY = os.getenv("API_KEY", "sk-web2api-placeholder")
DSML_ENABLED = os.getenv("DSML_ENABLED", "false").lower() in ("true", "1", "yes")

app = FastAPI(title="web2api", version="0.1.0")
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
    model: str = MODEL_NAME
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
                "id": MODEL_NAME,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "web2api",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
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
    return {"status": "ok", "target": TARGET_URL, "model": MODEL_NAME}


if __name__ == "__main__":
    import sys
    port = PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    print(f"web2api proxy running on http://{HOST}:{port}")
    print(f"  Target: {TARGET_URL}")
    print(f"  Model:  {MODEL_NAME}")
    print(f"\n  Test with:")
    print(f'    curl http://localhost:{port}/v1/chat/completions \\')
    print(f'      -H "Content-Type: application/json" \\')
    print(f'      -d \'{{"model":"{MODEL_NAME}","messages":[{{"role":"user","content":"Hello"}}]}}\'')
    uvicorn.run(app, host=HOST, port=port)
