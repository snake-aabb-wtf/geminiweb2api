"""Tests for ``adapter.build_request_body``."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, unquote

import pytest

from account_pool import Account
from adapter import ModelProfile, build_request_body, INNER_PADDING, IDX_MODEL_FAMILY, IDX_THINKING_MODE


def _account() -> Account:
    return Account(
        name="t", f_sid="fsid123", at="attoken", sn_param="sn123",
        bl_param="blver", hl="zh-CN", session_uuid="uuid", request_hash="hash",
    )


def _profile() -> ModelProfile:
    return ModelProfile(name="gem", model_family=3, thinking_mode=2)


def test_inner_array_is_padded_to_expected_length():
    profile = _profile()
    body, _query, _new_reqid = build_request_body(profile, _account(), [{"role": "user", "content": "hi"}], 0)
    # body is urlencoded; we recover f.req via unquote.
    freq = parse_qs(body)["f.req"][0]
    decoded = unquote(freq)
    outer = json.loads(decoded)
    inner = json.loads(outer[1])
    assert len(inner) == INNER_PADDING
    assert inner[IDX_MODEL_FAMILY] == 3
    assert inner[IDX_THINKING_MODE] == 2


def test_reqid_increments_monotonically():
    profile = _profile()
    _, _, new1 = build_request_body(profile, _account(), [{"role": "user", "content": "a"}], 100)
    _, _, new2 = build_request_body(profile, _account(), [{"role": "user", "content": "b"}], new1)
    assert new2 == new1 + 1


def test_url_query_contains_required_params():
    profile = _profile()
    _body, query, _ = build_request_body(profile, _account(), [{"role": "user", "content": "x"}], 0)
    qs = parse_qs(query)
    assert qs["bl"] == ["blver"]
    assert qs["f.sid"] == ["fsid123"]
    assert qs["hl"] == ["zh-CN"]
    assert qs["rt"] == ["c"]
    assert "_reqid" in qs


def test_message_with_list_content_flattened():
    profile = _profile()
    content = [
        {"type": "text", "text": "hello"},
        {"type": "text", "text": "world"},
    ]
    body, _query, _ = build_request_body(profile, _account(), [{"role": "user", "content": content}], 0)
    freq = parse_qs(body)["f.req"][0]
    decoded = unquote(freq)
    outer = json.loads(decoded)
    inner = json.loads(outer[1])
    assert inner[0][0] == "hello world"


def test_image_url_attachment_preserved_in_meta():
    profile = _profile()
    content = [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    body, _q, _ = build_request_body(profile, _account(), [{"role": "user", "content": content}], 0)
    freq = parse_qs(body)["f.req"][0]
    decoded = unquote(freq)
    outer = json.loads(decoded)
    inner = json.loads(outer[1])
    meta = inner[2]
    # attachments are appended after the canonical 10 slots.
    assert any(isinstance(m, dict) and m.get("type") == "image_url" for m in meta)


def test_at_token_added_to_body_when_present():
    body, _, _ = build_request_body(_profile(), _account(), [{"role": "user", "content": "x"}], 0)
    qs = parse_qs(body)
    assert qs.get("at") == ["attoken"]


def test_at_token_omitted_when_empty():
    acct = _account()
    acct.at = ""
    body, _, _ = build_request_body(_profile(), acct, [{"role": "user", "content": "x"}], 0)
    qs = parse_qs(body)
    assert "at" not in qs
