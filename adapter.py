import json
import re
import time
import httpx
from dataclasses import dataclass, field
from urllib.parse import urlencode
from typing import AsyncGenerator, Optional

CHAT_ENDPOINT = "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"


@dataclass
class ModelProfile:
    name: str
    model_family: int = 1
    thinking_mode: int = 1
    f_sid: str = ""
    at: str = ""
    sn_param: str = ""
    bl_param: str = ""
    hl: str = "zh-CN"
    session_uuid: str = ""
    request_hash: str = ""
    headers: dict = field(default_factory=dict)


class ChatAdapter:
    def __init__(self, profiles: dict[str, ModelProfile], default_model: str):
        self.profiles = profiles
        self._active_name = default_model
        self._reqid = int(time.time() * 1000) % 10000000

    @property
    def _profile(self) -> ModelProfile:
        return self.profiles[self._active_name]

    def set_model(self, name: str) -> bool:
        if name in self.profiles:
            self._active_name = name
            return True
        return False

    @property
    def model_name(self) -> str:
        return self._active_name

    def _build_jspb_header(self, for_stream: bool = True) -> str:
        p = self._profile
        if for_stream:
            return json.dumps([
                1, None, None, None, p.request_hash or None, None, None, 0,
                [4, 5, 6, 8], None, None, 1, None, None,
                p.model_family, p.thinking_mode, p.session_uuid or None
            ], separators=(",", ":"))
        return json.dumps([
            1, None, None, None, None, None, None, None,
            [4, 5, 6, 8], None, None, None, None, None,
            p.model_family, p.thinking_mode, p.session_uuid or None
        ], separators=(",", ":"))

    def _build_request_body(self, messages: list, stream: bool = False) -> str:
        p = self._profile
        last = messages[-1]["content"] if messages else ""
        if isinstance(last, list):
            last = " ".join(item.get("text", "") for item in last if item.get("type") == "text")
        self._reqid += 1

        meta = ["", "", "", None, None, None, None, None, None, ""]
        inner = [
            [last, 0, None, None, None, None, 0],
            [p.hl],
            meta,
            p.sn_param,
        ]
        while len(inner) < 81:
            inner.append(None)
        inner[79] = p.model_family
        inner[80] = p.thinking_mode

        inner_json = json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
        outer = [None, inner_json]
        freq = json.dumps(outer, ensure_ascii=False, separators=(",", ":"))

        params = {
            "bl": p.bl_param,
            "f.sid": p.f_sid,
            "hl": p.hl,
            "_reqid": str(self._reqid),
            "rt": "c",
        }
        query_string = urlencode(params)
        body = {"f.req": freq}
        if p.at:
            body["at"] = p.at
        return urlencode(body), query_string

    def _parse_response(self, text: str) -> str:
        lines = text.strip().split("\n")
        contents = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith(")]}'") or line.isdigit():
                continue
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(data, list) or len(data) == 0:
                continue
            wrb = data[0]
            if not isinstance(wrb, list) or len(wrb) < 3:
                continue
            third = wrb[2]
            if not isinstance(third, str):
                continue
            try:
                inner_data = json.loads(third)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(inner_data, list) or len(inner_data) < 5:
                continue
            fourth = inner_data[4]
            if not isinstance(fourth, list):
                continue
            for item in fourth:
                if not isinstance(item, list) or len(item) < 2:
                    continue
                content_parts = item[1]
                if not isinstance(content_parts, list):
                    continue
                for part in content_parts:
                    if isinstance(part, str) and len(part) > 0:
                        contents.append(part)
        if not contents:
            return ""
        return max(contents, key=len)

    def convert_request(self, messages: list, stream: bool = False, tools: Optional[list] = None, tool_choice: Optional[str] = None, **kwargs) -> dict:
        body, query = self._build_request_body(messages, stream)
        return {"body": body, "query": query}

    def _make_headers(self) -> dict:
        p = self._profile
        headers = dict(p.headers)
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
        headers["x-goog-ext-525001261-jspb"] = self._build_jspb_header(for_stream=True)
        return headers

    async def send_request(self, payload: dict) -> dict:
        query = payload.get("query", "")
        body = payload.get("body", "")
        url = f"https://gemini.google.com{CHAT_ENDPOINT}?{query}"
        headers = self._make_headers()
        async with httpx.AsyncClient(headers=headers, timeout=120) as client:
            resp = await client.post(url, data=body)
            resp.raise_for_status()
            content = self._parse_response(resp.text)
            return self.convert_response({"text": content})

    async def stream_request(self, payload: dict) -> AsyncGenerator[bytes, None]:
        query = payload.get("query", "")
        body = payload.get("body", "")
        url = f"https://gemini.google.com{CHAT_ENDPOINT}?{query}"
        headers = self._make_headers()
        async with httpx.AsyncClient(headers=headers, timeout=120) as client:
            resp = await client.post(url, data=body)
            resp.raise_for_status()
            content = self._parse_response(resp.text)
            if content:
                yield self._build_content_chunk(content)
            yield b"data: [DONE]\n\n"

    def _build_content_chunk(self, text: str) -> bytes:
        chunk = {"choices": [{"delta": {"content": text}, "index": 0}]}
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

    def convert_response(self, response: dict) -> dict:
        content = response.get("text") or response.get("content") or json.dumps(response)
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self._active_name,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
