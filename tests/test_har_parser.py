"""Tests for ``har_parser.parse_har``."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from har_parser import parse_har


def _build_har(jspb: list, inner: list, *, bl="boq_bl", f_sid="fsid", hl="zh-CN", at="at123") -> dict:
    """Build a minimal HAR dict containing a single StreamGenerate entry."""
    jspb_str = json.dumps(jspb, separators=(",", ":"))
    inner_str = json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
    outer = [None, inner_str]
    freq = json.dumps(outer, ensure_ascii=False, separators=(",", ":"))
    body = f"f.req={freq}&at={at}"
    url = (
        "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={bl}&f.sid={f_sid}&hl={hl}&_reqid=1&rt=c"
    )
    return {
        "log": {
            "entries": [
                {
                    "request": {
                        "url": url,
                        "headers": [
                            {"name": "x-goog-ext-525001261-jspb", "value": jspb_str},
                            {"name": "user-agent", "value": "Mozilla/5.0"},
                            {"name": "cookie", "value": "should-be-skipped"},
                        ],
                        "postData": {"text": body},
                    },
                    "response": {"content": {"text": ""}},
                }
            ]
        }
    }


def _inner_with(family: int = 1, thinking: int = 1, sn: str = "sn123") -> list:
    inner = [["hi", 0, None, None, None, None, 0], ["zh-CN"], ["", "", "", None, None, None, None, None, None, ""], sn]
    while len(inner) < 81:
        inner.append(None)
    inner[79] = family
    inner[80] = thinking
    return inner


def test_parses_url_query_params(tmp_path: Path):
    har = _build_har([0]*17, _inner_with(), bl="boq_x", f_sid="Fsid", hl="en-US")
    p = tmp_path / "x.har"
    p.write_text(json.dumps(har), encoding="utf-8")
    analysis = parse_har(str(p))
    assert analysis.bl_param == "boq_x"
    assert analysis.f_sid == "Fsid"
    assert analysis.hl == "en-US"


def test_parses_jspb_header_indices(tmp_path: Path):
    # Indices 4, 14, 15, 16 → request_hash, model_family, thinking_mode, session_uuid.
    # The inner body must NOT carry family/thinking for JSPB values to win.
    jspb = [0] * 17
    jspb[4] = "reqhash"
    jspb[14] = 6
    jspb[15] = 2
    jspb[16] = "session-uuid-xyz"
    # Build inner that has family/thinking as None so the parser falls back to JSPB.
    inner = [["hi"], ["zh-CN"], [""] * 10, "sn"]
    inner = inner + [None] * (81 - len(inner))
    har = _build_har(jspb, inner)
    p = tmp_path / "x.har"
    p.write_text(json.dumps(har), encoding="utf-8")
    analysis = parse_har(str(p))
    assert analysis.request_hash == "reqhash"
    assert analysis.model_family == 6
    assert analysis.thinking_mode == 2
    assert analysis.session_uuid == "session-uuid-xyz"


def test_inner_array_overrides_jspb(tmp_path: Path):
    """The inner array is the authoritative source; JSPB is fallback."""
    jspb = [0] * 17
    jspb[14] = 1
    jspb[15] = 1
    inner = _inner_with(family=3, thinking=2, sn="sn-from-body")
    har = _build_har(jspb, inner)
    p = tmp_path / "x.har"
    p.write_text(json.dumps(har), encoding="utf-8")
    analysis = parse_har(str(p))
    assert analysis.model_family == 3
    assert analysis.thinking_mode == 2
    assert analysis.sn_param == "sn-from-body"


def test_at_token_extracted_from_body(tmp_path: Path):
    har = _build_har([0]*17, _inner_with(), at="at-from-body")
    p = tmp_path / "x.har"
    p.write_text(json.dumps(har), encoding="utf-8")
    analysis = parse_har(str(p))
    assert analysis.at == "at-from-body"


def test_hop_by_hop_headers_skipped(tmp_path: Path):
    har = _build_har([0]*17, _inner_with())
    p = tmp_path / "x.har"
    p.write_text(json.dumps(har), encoding="utf-8")
    analysis = parse_har(str(p))
    # cookie was excluded; user-agent kept; jspb not stored under "headers".
    assert "cookie" not in {h.lower() for h in analysis.headers}
    assert "x-goog-ext-525001261-jspb" not in analysis.headers
    assert "user-agent" in analysis.headers
