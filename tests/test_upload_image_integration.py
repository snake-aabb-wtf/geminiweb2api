"""Tests that ``/v1/chat/completions`` actually invokes ``upload_image``
when the request carries ``image_url`` content.

The pre-v1.1 code accepted image content but never uploaded it,
so the upstream model never saw the image. These tests pin that
behaviour in place.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import dotenv
import httpx
import pytest


def _reload_server(env_path: Path):
    dotenv.load_dotenv(env_path, override=True)
    import server
    importlib.reload(server)
    return server


@pytest.fixture
def server_module(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("PROFILES=g\nDEFAULT_MODEL=g\n", encoding="utf-8")
    monkeypatch.setenv("ENV_PATH", str(env))
    for var in ("API_KEY", "ADMIN_KEY", "API_KEYS", "ADMIN_KEYS"):
        monkeypatch.delenv(var, raising=False)
    return _reload_server(env)


@pytest.mark.asyncio
async def test_chat_with_image_url_invokes_upload(monkeypatch, server_module):
    from account_pool import Account
    server_module.pool.add(Account(
        name="a", f_sid="s", at="t", sn_param="n", bl_param="b", bound_models=["g"],
    ))

    upload_calls: list[str] = []
    async def fake_upload(account, image_url, extra_headers=None):
        upload_calls.append(image_url)
        return "UPLOADED_ID_42"

    async def fake_send(profile, account, messages, reqid, tools=None, attachments=None):
        # Verify that attachments contains our upload_id.
        assert attachments is not None
        assert any(a.get("value") == "UPLOADED_ID_42" for a in attachments)
        return {
            "id": "chatcmpl-x",
            "object": "chat.completion",
            "created": 0,
            "model": "g",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }, reqid + 1, 200

    monkeypatch.setattr("adapter.upload_image", fake_upload)
    monkeypatch.setattr("adapter.send_request", fake_send)

    tr = httpx.ASGITransport(app=server_module.app)
    async with httpx.AsyncClient(transport=tr, base_url="http://t") as client:
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": "g",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                }],
            },
        )
    assert r.status_code == 200
    assert len(upload_calls) == 1
    assert upload_calls[0].startswith("data:image/png")


@pytest.mark.asyncio
async def test_chat_without_image_does_not_call_upload(monkeypatch, server_module):
    from account_pool import Account
    server_module.pool.add(Account(
        name="a", f_sid="s", at="t", sn_param="n", bl_param="b", bound_models=["g"],
    ))

    upload_calls: list[str] = []
    async def fake_upload(*args, **kwargs):
        upload_calls.append("called")
        return "should_not"

    async def fake_send(profile, account, messages, reqid, tools=None, attachments=None):
        return {
            "id": "x", "object": "chat.completion", "created": 0, "model": "g",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }, reqid + 1, 200

    monkeypatch.setattr("adapter.upload_image", fake_upload)
    monkeypatch.setattr("adapter.send_request", fake_send)

    tr = httpx.ASGITransport(app=server_module.app)
    async with httpx.AsyncClient(transport=tr, base_url="http://t") as client:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "g", "messages": [{"role": "user", "content": "plain text"}]},
        )
    assert r.status_code == 200
    assert upload_calls == []  # never called


@pytest.mark.asyncio
async def test_chat_with_image_upload_failure_continues(monkeypatch, server_module):
    """If upload fails the request still completes with the text part."""
    from account_pool import Account
    server_module.pool.add(Account(
        name="a", f_sid="s", at="t", sn_param="n", bl_param="b", bound_models=["g"],
    ))

    async def fake_upload(*args, **kwargs):
        return None  # failure

    async def fake_send(profile, account, messages, reqid, tools=None, attachments=None):
        # attachments is either [] or None — the image was dropped, not fatal.
        assert not attachments
        return {
            "id": "x", "object": "chat.completion", "created": 0, "model": "g",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }, reqid + 1, 200

    monkeypatch.setattr("adapter.upload_image", fake_upload)
    monkeypatch.setattr("adapter.send_request", fake_send)

    tr = httpx.ASGITransport(app=server_module.app)
    async with httpx.AsyncClient(transport=tr, base_url="http://t") as client:
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": "g",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "see?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                }],
            },
        )
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "ok"
