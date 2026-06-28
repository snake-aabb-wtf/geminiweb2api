"""HAR → Gemini credential extraction.

The reverse-engineered Gemini web endpoint requires a handful of opaque
tokens that can only be obtained by inspecting a real browser session.
The standard way to capture them is to export a Network log as a
``.har`` file while a normal Gemini conversation is in flight, and
point this module at it.

Only the ``StreamGenerate`` request entry is interesting; we extract:

* URL query parameters: ``bl``, ``f.sid``, ``hl``, ``_reqid``, ``rt``
* ``at`` token (from POST body, ``&at=`` suffix)
* ``sn_param`` (from ``f.req`` inner array slot 3)
* ``model_family`` / ``thinking_mode`` (from ``f.req`` inner slots
  79/80 *or* the ``x-goog-ext-525001261-jspb`` header)
* ``session_uuid`` / ``request_hash`` (from the same JSPB header)

The original implementation grew a few dead fields (``template_params``,
``all_endpoints``, ``content_field_path``) that no caller uses; they
have been removed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

CHAT_ENDPOINT_SUBSTR = "StreamGenerate"

# JSPB array indices — Google does not publish these, but they have
# been stable for many months. Update both lookup sites in lockstep.
JSPB_IDX_REQUEST_HASH = 4
JSPB_IDX_MODEL_FAMILY = 14
JSPB_IDX_THINKING_MODE = 15
JSPB_IDX_SESSION_UUID = 16

# Inner-array indices inside the body.
INNER_IDX_USER_TURN = 0
INNER_IDX_LANGUAGE = 1
INNER_IDX_META = 2
INNER_IDX_SN_PARAM = 3
INNER_IDX_MODEL_FAMILY = 79
INNER_IDX_THINKING_MODE = 80

# Headers we never want to copy verbatim from the HAR.
_HOP_BY_HOP = {":method", ":path", ":authority", ":scheme", "content-length", "cookie"}


@dataclass
class GeminiHarAnalysis:
    """Structured result of parsing a HAR file."""

    base_url: str = "https://gemini.google.com"
    chat_endpoint: str = "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
    headers: dict = field(default_factory=dict)
    bl_param: str = ""
    f_sid: str = ""
    hl: str = "zh-CN"
    at: str = ""
    sn_param: str = ""
    model_family: int = 1
    thinking_mode: int = 1
    session_uuid: str = ""
    request_hash: str = ""

    def __post_init__(self) -> None:
        # ``headers`` is initialised via ``field(default_factory=...)``
        # so we no longer need to coerce ``None`` here; the method is
        # kept for backwards compatibility.
        pass


def parse_har(har_path: str) -> GeminiHarAnalysis:
    """Parse ``har_path`` and return a :class:`GeminiHarAnalysis`."""
    with open(har_path, "r", encoding="utf-8") as fh:
        har = json.load(fh)
    entries = har.get("log", {}).get("entries", [])
    chat_entry = _find_chat_entry(entries)
    if chat_entry is None:
        raise ValueError("HAR file does not contain a StreamGenerate request")

    analysis = GeminiHarAnalysis()
    req = chat_entry.get("request", {})
    _extract_headers(analysis, req.get("headers", []))
    _extract_url_params(analysis, req.get("url", ""))
    _extract_post_body(analysis, req.get("postData", {}).get("text", ""))
    return analysis


def _find_chat_entry(entries: list) -> Optional[dict]:
    for entry in entries:
        url = entry.get("request", {}).get("url", "")
        if CHAT_ENDPOINT_SUBSTR in url:
            return entry
    return entries[0] if entries else None


def _extract_headers(analysis: GeminiHarAnalysis, headers: list) -> None:
    for h in headers:
        name = h.get("name", "")
        val = h.get("value", "")
        lname = name.lower()
        if lname in _HOP_BY_HOP:
            continue
        if lname == "x-goog-ext-525001261-jspb":
            _parse_jspb(analysis, val)
            continue
        analysis.headers[name] = val


def _extract_url_params(analysis: GeminiHarAnalysis, url: str) -> None:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    analysis.bl_param = qs.get("bl", [""])[0]
    analysis.f_sid = qs.get("f.sid", [""])[0]
    analysis.hl = qs.get("hl", [analysis.hl])[0]


def _parse_jspb(analysis: GeminiHarAnalysis, val: str) -> None:
    try:
        arr = json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(arr, list):
        return
    if len(arr) > JSPB_IDX_MODEL_FAMILY and isinstance(arr[JSPB_IDX_MODEL_FAMILY], int):
        analysis.model_family = arr[JSPB_IDX_MODEL_FAMILY]
    if len(arr) > JSPB_IDX_THINKING_MODE and isinstance(arr[JSPB_IDX_THINKING_MODE], int):
        analysis.thinking_mode = arr[JSPB_IDX_THINKING_MODE]
    if len(arr) > JSPB_IDX_SESSION_UUID and isinstance(arr[JSPB_IDX_SESSION_UUID], str):
        analysis.session_uuid = arr[JSPB_IDX_SESSION_UUID]
    if len(arr) > JSPB_IDX_REQUEST_HASH and isinstance(arr[JSPB_IDX_REQUEST_HASH], str):
        analysis.request_hash = arr[JSPB_IDX_REQUEST_HASH]


def _extract_post_body(analysis: GeminiHarAnalysis, text: str) -> None:
    if not text or "f.req=" not in text:
        return
    parsed_body = parse_qs(text)
    freq = parsed_body.get("f.req", [""])[0]
    decoded = unquote(freq)
    try:
        data = json.loads(decoded)
        if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], str):
            inner = json.loads(data[1])
            _extract_inner_fields(analysis, inner)
    except (json.JSONDecodeError, TypeError, IndexError):
        pass

    # ``&at=`` typically appears in the same body string.
    if "&at=" in text:
        m = re.search(r"&at=([^&]+)", text)
        if m:
            analysis.at = m.group(1)


def _extract_inner_fields(analysis: GeminiHarAnalysis, inner: list) -> None:
    if not isinstance(inner, list) or len(inner) <= INNER_IDX_SN_PARAM:
        return
    sn = inner[INNER_IDX_SN_PARAM]
    if isinstance(sn, str):
        analysis.sn_param = sn
    if len(inner) > INNER_IDX_MODEL_FAMILY and inner[INNER_IDX_MODEL_FAMILY] is not None:
        analysis.model_family = inner[INNER_IDX_MODEL_FAMILY]
    if len(inner) > INNER_IDX_THINKING_MODE and inner[INNER_IDX_THINKING_MODE] is not None:
        analysis.thinking_mode = inner[INNER_IDX_THINKING_MODE]
