"""Gemini-Web protocol adapter.

What this module is responsible for
-----------------------------------
* Building the ``x-goog-ext-525001261-jspb`` header that selects the model
  family / thinking mode on Gemini's internal StreamGenerate endpoint.
* Building the ``f.req=`` POST body — a double-encoded JSPB array whose
  tail positions ``[79]`` and ``[80]`` set the model family and thinking
  mode.
* Parsing Gemini's batch-style ``)]}'`` response. The response is *not*
  a real SSE stream: the entire payload is one buffer split by newlines
  that contains interleaved JSON arrays, so the streaming variant just
  chunks that buffer for the client to give the illusion of progress.
* Token-usage extraction from ``inner_data[2]`` (where Gemini embeds
  ``_mtokenCount``/``_ttokenCount``/``_stokenCount``).
* Function-calling detection (best-effort — Gemini Web does not
  consistently expose tool calls, so failures degrade gracefully).
* Multipart image upload via the UploadFile endpoint, returning an
  ``upload_id`` to embed in the next conversation turn.

Reliability
-----------
The public coroutines acquire a shared ``httpx.AsyncClient`` from
``get_client()`` so TLS handshakes are amortised. Timeouts are
per-stage so a slow read does not eat the connect budget.
"""
from __future__ import annotations

import base64
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Iterable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from logger import get_logger

log = get_logger("adapter")

__version__ = "1.0.0"

CHAT_ENDPOINT = "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
UPLOAD_ENDPOINT = "/_/BardChatUi/data/assistant.lamda.BardFrontendService/UploadFile"
BASE_URL = "https://gemini.google.com"

# Default per-stage timeouts (seconds). Streaming gets a more generous read.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0)
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)

# Inner-array padding target — Gemini rejects requests shorter than this.
INNER_PADDING = 81
# Indices inside the inner array that carry the protocol fields.
IDX_MODEL_FAMILY = 79
IDX_THINKING_MODE = 80


# ── Profile ──────────────────────────────────────────────────────────

@dataclass
class ModelProfile:
    """A user-facing model name → Gemini protocol parameters."""

    name: str
    model_family: int = 1     # 1 = Flash, 3 = Pro, 6 = Flash Lite
    thinking_mode: int = 1    # 1 = standard, 2 = advanced
    # Free-form temperature / max_tokens hints that get into inner[2].
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # Optional system prompt override, sent as the first user message.
    system_prompt: str = ""

    def __post_init__(self) -> None:
        if not (1 <= self.model_family <= 9):
            raise ValueError(f"model_family out of range: {self.model_family}")
        if self.thinking_mode not in (1, 2):
            raise ValueError(f"thinking_mode must be 1 or 2, got {self.thinking_mode}")


# ── Shared HTTP client ───────────────────────────────────────────────

_client: Optional[httpx.AsyncClient] = None
_client_lock_holder: list = []   # Placeholder for any future locking.


async def get_client() -> httpx.AsyncClient:
    """Return a process-wide shared ``httpx.AsyncClient`` (lazy)."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 gemini2api/1.0"},
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ── Header / body construction ───────────────────────────────────────

def build_jspb_header(
    model_family: int,
    thinking_mode: int,
    session_uuid: str = "",
    request_hash: str = "",
    for_stream: bool = True,
) -> str:
    """Render the 17-element ``x-goog-ext-525001261-jspb`` array.

    The shape is asymmetric between stream and non-stream callers — the
    streaming form needs the ``[4, 5, 6, 8]`` capability set and the
    request hash, while the non-streaming form keeps the slot empty.
    """
    if for_stream:
        return json.dumps([
            1, None, None, None, request_hash or None, None, None, 0,
            [4, 5, 6, 8], None, None, 1, None, None,
            model_family, thinking_mode, session_uuid or None,
        ], separators=(",", ":"))
    return json.dumps([
        1, None, None, None, None, None, None, None,
        [4, 5, 6, 8], None, None, None, None, None,
        model_family, thinking_mode, session_uuid or None,
    ], separators=(",", ":"))


def _flatten_content(content: Any) -> tuple[str, list[dict]]:
    """Return ``(text, attachments)`` for a message content value.

    Supports plain strings, ``[{"type": "text", ...}, {"type": "image_url", ...}]``
    OpenAI-style arrays, and bare URLs. Attachments are *not* uploaded here —
    that is the caller's job (see ``upload_image``).
    """
    if isinstance(content, str):
        return content, []
    if isinstance(content, list):
        text_parts: list[str] = []
        attachments: list[dict] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                if part.get("text"):
                    text_parts.append(part["text"])
            elif ptype == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url:
                    attachments.append({"type": "image_url", "url": url})
        return " ".join(t for t in text_parts if t), attachments
    return str(content), []


def _meta_block(profile: ModelProfile, attachments: list[dict], tools: Optional[list] = None) -> list:
    """Compose ``inner[2]`` — the meta slot, length-10 by convention.

    Slots used:
        0,1,2,3,4,5,6,7,8,9  — Gemini Web pads with empty strings/None.
        We append attachments (upload_ids) and the tools manifest as extras
        *after* the canonical 10 slots so we don't break compatibility.
    """
    meta: list = ["", "", "", None, None, None, None, None, None, ""]
    # Attachments become additional elements appended after meta.
    if attachments:
        meta.extend(attachments)
    if tools:
        # Gemini Web does not have a stable tools field; we stash a
        # serialised copy that the response parser can match against.
        meta.append({"__tools__": tools})
    if profile.temperature is not None or profile.max_tokens is not None:
        meta.append({
            "__sampling__": {
                "temperature": profile.temperature,
                "max_tokens": profile.max_tokens,
            }
        })
    return meta


def build_request_body(
    profile: ModelProfile,
    account,
    messages: list[dict],
    reqid: int,
    tools: Optional[list] = None,
    attachments: Optional[list[dict]] = None,
) -> tuple[str, str, int]:
    """Build the ``f.req=`` body + URL query string for one request.

    The body is a double-JSON-encoded structure: outer ``[None, "<inner>"]``
    urlencoded into ``f.req``. The inner array is padded to ``INNER_PADDING``
    elements, with ``model_family`` and ``thinking_mode`` at the end.
    """
    reqid += 1
    user_text, msg_attachments = _flatten_content(
        messages[-1]["content"] if messages else ""
    )
    if profile.system_prompt and not any(m["role"] == "system" for m in messages):
        user_text = f"{profile.system_prompt}\n\n{user_text}" if user_text else profile.system_prompt
    # Combine caller-supplied attachments with those extracted from the
    # current message body.
    all_attachments = list(attachments or []) + msg_attachments

    meta = _meta_block(profile, all_attachments, tools)

    # inner[0] is the user turn: [text, conv_id, ?, ?, ?, ?, role_flag]
    inner: list[Any] = [
        [user_text, 0, None, None, None, None, 0],
        [account.hl],
        meta,
        account.sn_param,
    ]
    while len(inner) < INNER_PADDING:
        inner.append(None)
    inner[IDX_MODEL_FAMILY] = profile.model_family
    inner[IDX_THINKING_MODE] = profile.thinking_mode

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
    body: dict[str, str] = {"f.req": freq}
    if account.at:
        body["at"] = account.at
    return urlencode(body), query_string, reqid


# ── Response parsing ─────────────────────────────────────────────────

@dataclass
class ParsedResponse:
    content: str = ""
    reasoning: str = ""         # For thinking_mode=2 (best-effort).
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    raw_inner: Optional[list] = None  # For tests / debugging.

    def to_openai_message(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.reasoning:
            # OpenAI o1-style field — clients that don't know about it
            # will simply ignore the key.
            msg["reasoning_content"] = self.reasoning
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg


def _extract_text_fragments(inner_data: list) -> list[list[str]]:
    """Walk ``inner_data[4]`` and yield per-candidate text-part lists.

    Each candidate looks like ``[rc_id, [text_part_1, text_part_2, ...]]``.
    """
    if len(inner_data) < 5 or not isinstance(inner_data[4], list):
        return []
    out: list[list[str]] = []
    for item in inner_data[4]:
        if not isinstance(item, list) or len(item) < 2:
            continue
        parts = item[1]
        if isinstance(parts, list):
            out.append([p for p in parts if isinstance(p, str) and p])
    return out


def _detect_tool_calls(inner_data: list) -> list[dict]:
    """Best-effort extraction of function calls from the answer candidates.

    Gemini Web does not expose a stable tool-call schema — when a tool
    fires, the call payload tends to live alongside the answer text in
    ``inner_data[4]`` but its exact position varies. We scan every
    dict-looking slot inside the candidate entries and pick up any
    object that carries ``name`` + ``arguments`` (the OpenAI shape).
    """
    if len(inner_data) < 5 or not isinstance(inner_data[4], list):
        return []
    calls: list[dict] = []
    for item in inner_data[4]:
        if not isinstance(item, list):
            continue
        for slot in item:
            entries: list = []
            if isinstance(slot, list):
                entries = [s for s in slot if isinstance(s, dict)]
            elif isinstance(slot, dict):
                entries = [slot]
            for entry in entries:
                if "name" in entry and "arguments" in entry:
                    args = entry["arguments"]
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False)
                    calls.append({
                        "id": f"call_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {"name": entry["name"], "arguments": args},
                    })
    return calls


def _parse_usage(inner_data: list) -> dict:
    """Pull token counts from ``inner_data[2]`` if present.

    The web endpoint tends to embed a dict with keys like ``_mtokenCount``
    (model tokens), ``_stokenCount`` (system tokens), and ``_ttokenCount``
    (total). We translate these into the OpenAI ``usage`` shape with safe
    fallbacks for missing fields.
    """
    if len(inner_data) < 3 or not isinstance(inner_data[2], dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    meta = inner_data[2]
    try:
        # Total ≈ model + system; completion is whatever Gemini calls
        # ``_mtokenCount`` (model tokens spent on the response).
        mtoken = int(meta.get("_mtokenCount", 0) or 0)
        stoken = int(meta.get("_stokenCount", 0) or 0)
        ttoken = int(meta.get("_ttokenCount", 0) or 0)
        completion = mtoken
        prompt = max(0, ttoken - completion)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": ttoken or (prompt + completion),
        }
    except (TypeError, ValueError):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def parse_response(text: str) -> ParsedResponse:
    """Parse a single Gemini batch response into a ``ParsedResponse``."""
    result = ParsedResponse()
    if not text:
        return result

    # Gemini prepends an anti-XSSI prefix and the body is multi-line JSON.
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(")]}'") or line.isdigit():
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, list) or not data:
            continue
        wrb = data[0]
        if not isinstance(wrb, list) or len(wrb) < 3 or not isinstance(wrb[2], str):
            continue
        try:
            inner = json.loads(wrb[2])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(inner, list):
            continue
        result.raw_inner = inner
        result.usage = _parse_usage(inner)
        result.tool_calls = _detect_tool_calls(inner)
        candidates = _extract_text_fragments(inner)
        if not candidates:
            continue
        # Primary answer: first non-empty candidate, joined.
        primary_parts = candidates[0]
        if primary_parts:
            result.content = "".join(primary_parts)
        # Reasoning: if thinking_mode=2, the second candidate is often the
        # chain-of-thought. We only treat it as reasoning when there are
        # *two* non-empty candidates and the first looks like a "final" answer.
        if len(candidates) >= 2 and result.content:
            second = "".join(candidates[1])
            if second and second != result.content:
                result.reasoning = second
    return result


# ── OpenAI shape helpers ─────────────────────────────────────────────

def _chat_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def build_content_chunk(text: str, *, role: str = "assistant", finish: Optional[str] = None) -> bytes:
    chunk = {
        "id": _chat_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "",
        "choices": [{"index": 0, "delta": {"role": role, "content": text}, "finish_reason": finish}],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()


def build_done_chunk() -> bytes:
    return b"data: [DONE]\n\n"


def convert_response(parsed: ParsedResponse, model_name: str) -> dict:
    return {
        "id": _chat_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": parsed.to_openai_message(),
            "finish_reason": "tool_calls" if parsed.tool_calls else "stop",
        }],
        "usage": parsed.usage,
    }


# ── Image upload (best-effort) ───────────────────────────────────────

_UPLOAD_URL = f"{BASE_URL}{UPLOAD_ENDPOINT}"


async def upload_image(
    account,
    image_url: str,
    extra_headers: Optional[dict] = None,
) -> Optional[str]:
    """Upload an image to Gemini and return the ``upload_id``.

    Supports three input shapes:
        * ``data:image/png;base64,...``  — decode inline
        * ``http(s)://``                 — fetch with httpx and forward
        * local filesystem path          — read & base64-encode

    Returns ``None`` on failure (the caller should log and continue
    without the image rather than failing the whole request).
    """
    try:
        if image_url.startswith("data:"):
            match = re.match(r"data:[^;]+;base64,(.+)", image_url, re.DOTALL)
            if not match:
                return None
            raw = base64.b64decode(match.group(1))
        elif image_url.startswith(("http://", "https://")):
            client = await get_client()
            r = await client.get(image_url, timeout=30)
            r.raise_for_status()
            raw = r.content
        else:
            # Treat as filesystem path; only attempt if it exists and is readable.
            from pathlib import Path
            p = Path(image_url)
            if not p.is_file():
                log.warning("upload_path_missing", extra={"path": image_url})
                return None
            raw = p.read_bytes()

        # Build the multipart form. Gemini expects the file under the
        # field name ``file`` with a filename + mime-type guess.
        import mimetypes
        mime = mimetypes.guess_type(image_url)[0] or "image/png"
        files = {"file": (f"upload.{mime.split('/')[-1]}", raw, mime)}
        params = {
            "bl": account.bl_param,
            "f.sid": account.f_sid,
            "hl": account.hl,
            "rt": "c",
        }
        headers = make_headers(account)
        if extra_headers:
            headers.update(extra_headers)

        client = await get_client()
        resp = await client.post(f"{_UPLOAD_URL}?{urlencode(params)}", files=files, headers=headers, timeout=60)
        resp.raise_for_status()
        # The upload endpoint returns JSON containing an upload id; we
        # probe a few common shapes.
        try:
            payload = resp.json()
        except json.JSONDecodeError:
            return None
        for key in ("upload_id", "uploadId", "id", "name"):
            if isinstance(payload, dict) and payload.get(key):
                return str(payload[key])
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("upload_failed", extra={"err": str(exc), "url_prefix": image_url[:40]})
        return None


# ── Request helpers ──────────────────────────────────────────────────

def make_headers(account) -> dict:
    headers = dict(account.headers or {})
    headers.setdefault("Content-Type", "application/x-www-form-urlencoded;charset=UTF-8")
    return headers


def make_request_headers(profile: ModelProfile, account) -> dict:
    headers = make_headers(account)
    headers["x-goog-ext-525001261-jspb"] = build_jspb_header(
        profile.model_family, profile.thinking_mode,
        account.session_uuid, account.request_hash, for_stream=True,
    )
    return headers


# ── Public coroutines ────────────────────────────────────────────────

async def send_request(
    profile: ModelProfile,
    account,
    messages: list[dict],
    reqid: int,
    tools: Optional[list] = None,
    attachments: Optional[list[dict]] = None,
) -> tuple[dict, int, int]:
    """Non-streaming call. Returns ``(openai_response, new_reqid, status_code)``."""
    body, query, new_reqid = build_request_body(profile, account, messages, reqid, tools=tools, attachments=attachments)
    url = f"{BASE_URL}{CHAT_ENDPOINT}?{query}"
    headers = make_request_headers(profile, account)

    client = await get_client()
    resp = await client.post(url, content=body, headers=headers)
    if resp.status_code >= 400:
        # Surface the upstream body so callers can include it in the
        # error response to the user.
        try:
            err_payload = resp.json()
        except json.JSONDecodeError:
            err_payload = resp.text[:500]
        return (
            {"error": {"type": "upstream_error", "status": resp.status_code, "body": err_payload}},
            new_reqid,
            resp.status_code,
        )
    parsed = parse_response(resp.text)
    return convert_response(parsed, profile.name), new_reqid, resp.status_code


async def stream_request(
    profile: ModelProfile,
    account,
    messages: list[dict],
    reqid: int,
    tools: Optional[list] = None,
    attachments: Optional[list[dict]] = None,
) -> AsyncGenerator[bytes, None]:
    """Streaming call.

    Gemini's web endpoint is *not* an SSE source — the entire body comes
    back at once. We open the connection with ``client.stream`` to avoid
    loading it into memory all at once, parse the buffered text, then
    chunk it out as ``delta`` events so the client experiences real
    incremental progress. If ``content`` is empty we still emit a single
    empty-content chunk + ``[DONE]`` to keep the protocol contract.
    """
    body, query, _new_reqid = build_request_body(profile, account, messages, reqid, tools=tools, attachments=attachments)
    url = f"{BASE_URL}{CHAT_ENDPOINT}?{query}"
    headers = make_request_headers(profile, account)

    client = await get_client()
    text_chunks: list[str] = []

    try:
        async with client.stream("POST", url, content=body, headers=headers, timeout=STREAM_TIMEOUT) as resp:
            if resp.status_code >= 400:
                # Read the body to give a useful error, then bail.
                err_body = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                err_chunk = {
                    "error": {"type": "upstream_error", "status": resp.status_code, "body": err_body},
                }
                yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n".encode()
                yield build_done_chunk()
                return
            # Buffer chunks; parse when we see the trailing newline that
            # delimits a JSON object.
            buffer = ""
            async for chunk in resp.aiter_text():
                buffer += chunk
                # Stop once we have the full document (Gemini closes the
                # connection after one response).
                if buffer.count("\n") >= 4 or len(buffer) > 65536:
                    break
            parsed = parse_response(buffer)
        # Simulate token-level streaming by chunking the final content.
        full = parsed.content or ""
        if full:
            # Aim for ~20 chunks regardless of length so the UI feels
            # responsive on short answers and not laggy on long ones.
            target = max(1, min(64, len(full) // 12 or 1))
            step = max(1, len(full) // target)
            for i in range(0, len(full), step):
                yield build_content_chunk(full[i:i + step])
        else:
            yield build_content_chunk("")
        # Emit a final usage chunk so streaming clients can recover token
        # counts (matches OpenAI's behaviour when ``stream_options.include_usage``).
        if parsed.usage and any(parsed.usage.values()):
            usage_chunk = {
                "id": _chat_id(),
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": profile.name,
                "choices": [],
                "usage": parsed.usage,
            }
            yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n".encode()
        yield build_done_chunk()
    except httpx.HTTPError as exc:
        log.warning("stream_http_error", extra={"err": str(exc)})
        err = {"error": {"type": "stream_error", "message": str(exc)}}
        yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode()
        yield build_done_chunk()
