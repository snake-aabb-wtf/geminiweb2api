import json
import re
import time
import httpx
from dataclasses import dataclass
from urllib.parse import urlencode
from typing import AsyncGenerator, Optional

CHAT_ENDPOINT = "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"


@dataclass
class ModelProfile:
    name: str
    model_family: int = 1
    thinking_mode: int = 1


def build_jspb_header(model_family: int, thinking_mode: int,
                       session_uuid: str = "", request_hash: str = "",
                       for_stream: bool = True) -> str:
    if for_stream:
        return json.dumps([
            1, None, None, None, request_hash or None, None, None, 0,
            [4, 5, 6, 8], None, None, 1, None, None,
            model_family, thinking_mode, session_uuid or None
        ], separators=(",", ":"))
    return json.dumps([
        1, None, None, None, None, None, None, None,
        [4, 5, 6, 8], None, None, None, None, None,
        model_family, thinking_mode, session_uuid or None
    ], separators=(",", ":"))


def build_request_body(profile: ModelProfile, account, messages: list, reqid: int, stream: bool = False) -> tuple[str, str, int]:
    last = messages[-1]["content"] if messages else ""
    if isinstance(last, list):
        last = " ".join(item.get("text", "") for item in last if item.get("type") == "text")
    reqid += 1

    meta = ["", "", "", None, None, None, None, None, None, ""]
    inner = [
        [last, 0, None, None, None, None, 0],
        [account.hl],
        meta,
        account.sn_param,
    ]
    while len(inner) < 81:
        inner.append(None)
    inner[79] = profile.model_family
    inner[80] = profile.thinking_mode

    inner_json = json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
    outer = [None, inner_json]
    freq = json.dumps(outer, ensure_ascii=False, separators=(",", ":"))

    params = {
        "bl": account.bl_param,
        "f.sid": account.f_sid,
        "hl": account.hl,
        "_reqid": str(reqid),
        "rt": "c",
    }
    query_string = urlencode(params)
    body = {"f.req": freq}
    if account.at:
        body["at"] = account.at
    return urlencode(body), query_string, reqid


def parse_response(text: str) -> str:
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


def make_headers(account) -> dict:
    headers = dict(account.headers)
    headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
    headers["x-goog-ext-525001261-jspb"] = build_jspb_header(
        0, 0, account.session_uuid, account.request_hash, for_stream=True
    )
    return headers


def build_content_chunk(text: str) -> bytes:
    chunk = {"choices": [{"delta": {"content": text}, "index": 0}]}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()


def convert_response(content: str, model_name: str) -> dict:
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def send_request(profile: ModelProfile, account, messages: list, reqid: int) -> dict:
    body, query, new_reqid = build_request_body(profile, account, messages, reqid)
    url = f"https://gemini.google.com{CHAT_ENDPOINT}?{query}"
    headers = make_headers(account)
    # Patch JSPB with actual model values
    headers["x-goog-ext-525001261-jspb"] = build_jspb_header(
        profile.model_family, profile.thinking_mode,
        account.session_uuid, account.request_hash, for_stream=True
    )
    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        resp = await client.post(url, data=body)
        resp.raise_for_status()
        content = parse_response(resp.text)
        return convert_response(content, profile.name), new_reqid, resp.status_code


async def stream_request(profile: ModelProfile, account, messages: list, reqid: int) -> AsyncGenerator[bytes, None]:
    body, query, new_reqid = build_request_body(profile, account, messages, reqid)
    url = f"https://gemini.google.com{CHAT_ENDPOINT}?{query}"
    headers = make_headers(account)
    headers["x-goog-ext-525001261-jspb"] = build_jspb_header(
        profile.model_family, profile.thinking_mode,
        account.session_uuid, account.request_hash, for_stream=True
    )
    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        resp = await client.post(url, data=body)
        resp.raise_for_status()
        content = parse_response(resp.text)
        if content:
            yield build_content_chunk(content)
        yield b"data: [DONE]\n\n"
