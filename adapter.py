import json
import re
import time
import httpx
from urllib.parse import urlencode, parse_qs, quote
from typing import AsyncGenerator, Optional

CHAT_ENDPOINT = "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"

class ChatAdapter:
    def __init__(self, cookies: str, base_url: str, dsml_enabled: bool = True):
        self.headers = {}
        self.base_url = base_url.rstrip("/")
        self.chat_endpoint = CHAT_ENDPOINT
        self.cookies = cookies
        self.dsml_enabled = dsml_enabled
        self.dsml_ready = False
        self.auth_type = "none"

        self.bl_param = "boq_assistant-bard-web-server_20260525.09_p0"
        self.f_sid = ""
        self.hl = "zh-CN"
        self.at = ""
        self._reqid = int(time.time() * 1000) % 10000000
        self.rt = "c"
        self._sn_param = ""

    def set_har_params(self, analysis):
        self.bl_param = analysis.bl_param
        self.f_sid = analysis.f_sid
        self.hl = analysis.hl
        self._reqid = int(analysis._reqid) + 1
        self.rt = analysis.rt
        self.at = analysis.at
        self._sn_param = analysis.template_params.get("token", "")
        self.headers.update(analysis.headers)

    def _build_request_body(self, messages: list, stream: bool = False) -> str:
        last = messages[-1]["content"] if messages else ""
        if isinstance(last, list):
            last = " ".join(p.get("text", "") for p in last if p.get("type") == "text")
        self._reqid += 1

        inner = [
            [last, 0, None, None, None, None, 0],
            [self.hl],
            ["", "", "", None, None, None, None, None, None, ""],
            self._sn_param,
        ]
        inner_json = json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
        outer = [None, inner_json]
        freq = json.dumps(outer, ensure_ascii=False, separators=(",", ":"))

        params = {
            "bl": self.bl_param,
            "f.sid": self.f_sid,
            "hl": self.hl,
            "_reqid": str(self._reqid),
            "rt": self.rt,
        }
        query_string = urlencode(params)
        body = {"f.req": freq}
        if self.at:
            body["at"] = self.at
        return urlencode(body), query_string

    def _parse_response(self, text: str) -> str:
        lines = text.strip().split("\n")
        contents = []
        for i, line in enumerate(lines):
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
            if not isinstance(inner_data, list):
                continue

            if len(inner_data) < 5:
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

    async def send_request(self, payload: dict) -> dict:
        query = payload.get("query", "")
        body = payload.get("body", "")
        url = f"{self.base_url}{self.chat_endpoint}?{query}"
        headers = dict(self.headers)
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
        if self.cookies:
            headers["Cookie"] = self.cookies
        async with httpx.AsyncClient(headers=headers, timeout=120) as client:
            resp = await client.post(url, data=body)
            resp.raise_for_status()
            text = resp.text
            content = self._parse_response(text)
            return self.convert_response({"text": content})

    async def stream_request(self, payload: dict) -> AsyncGenerator[bytes, None]:
        query = payload.get("query", "")
        body = payload.get("body", "")
        url = f"{self.base_url}{self.chat_endpoint}?{query}"
        headers = dict(self.headers)
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
        if self.cookies:
            headers["Cookie"] = self.cookies
        async with httpx.AsyncClient(headers=headers, timeout=120) as client:
            resp = await client.post(url, data=body)
            resp.raise_for_status()
            text = resp.text
            content = self._parse_response(text)
            if content:
                yield self._build_content_chunk(content)
            yield b"data: [DONE]\n\n"

    def _extract_content_from_data(self, data: dict) -> Optional[str]:
        return data.get("text") or data.get("content") or None

    def _extract_content_from_json(self, data: dict) -> Optional[str]:
        return data.get("text") or data.get("content") or data.get("answer") or None

    def _build_content_chunk(self, text: str) -> bytes:
        chunk = {"choices": [{"delta": {"content": text}, "index": 0}]}
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

    def convert_response(self, response: dict) -> dict:
        content = response.get("text") or response.get("content") or json.dumps(response)
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "gpt-4o",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
