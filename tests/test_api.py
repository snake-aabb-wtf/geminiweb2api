"""Smoke tests that exercise the FastAPI app via ``httpx.ASGITransport``.

These tests use ``monkeypatch`` to replace the upstream HTTP client so we
do not hit ``gemini.google.com`` from CI. They cover the auth + error
path round-trips and don't validate Gemini's real protocol.
"""
from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx
import pytest


@pytest.fixture
def server_module(monkeypatch, tmp_path):
    """Reload ``server`` with an isolated ``.env`` and auth disabled."""
    env = tmp_path / ".env"
    env.write_text("PROFILES=g\nDEFAULT_MODEL=g\n", encoding="utf-8")
    monkeypatch.setenv("ENV_PATH", str(env))
    monkeypatch.setenv("API_KEY", "")  # auth disabled
    monkeypatch.setenv("ADMIN_KEY", "")
    # Force server to re-read .env on reload.
    import dotenv
    dotenv.load_dotenv(env, override=True)
    import server
    importlib.reload(server)
    return server


@pytest.fixture
def transport(server_module):
    return httpx.ASGITransport(app=server_module.app)


@pytest.mark.asyncio
async def test_health(server_module, transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["auth"]["api_key_status"] == "disabled"


@pytest.mark.asyncio
async def test_v1_models(server_module, transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/v1/models")
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "list"
    assert any(m["id"] == "g" for m in data["data"])


@pytest.mark.asyncio
async def test_chat_completions_no_account_returns_502(server_module, transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "g", "messages": [{"role": "user", "content": "hi"}]},
        )
    # No accounts bound to model "g" → upstream_error 502.
    assert r.status_code == 502
    body = r.json()
    assert body["error"]["type"] == "upstream_error"


@pytest.mark.asyncio
async def test_chat_completions_invalid_model(server_module, transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "nope", "messages": [{"role": "user", "content": "x"}]},
        )
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_model"


@pytest.mark.asyncio
async def test_auth_required_when_key_set(monkeypatch, transport):
    # Re-load server with auth on, then reuse the existing transport.
    monkeypatch.setenv("API_KEY", "sk-real")
    import auth as auth_module
    importlib.reload(auth_module)
    import server
    importlib.reload(server)
    tr = httpx.ASGITransport(app=server.app)
    try:
        async with httpx.AsyncClient(transport=tr, base_url="http://t") as client:
            r = await client.get("/v1/models")
        assert r.status_code == 401
    finally:
        monkeypatch.setenv("API_KEY", "")
        importlib.reload(auth_module)
        importlib.reload(server)


@pytest.mark.asyncio
async def test_chat_completions_happy_path(monkeypatch, server_module, transport):
    """Wire up a fake account + fake Gemini response."""
    from account_pool import Account
    server_module.pool.add(Account(
        name="a", f_sid="s", at="a", sn_param="n", bl_param="b", hl="zh-CN",
        session_uuid="u", request_hash="h", bound_models=["g"],
    ))

    fake_response = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "g",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }

    async def fake_send_request(profile, account, messages, reqid, tools=None, attachments=None):
        return fake_response, reqid + 1, 200

    monkeypatch.setattr(server_module, "send_request", fake_send_request)

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "g", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "ok"
    assert body["usage"]["completion_tokens"] == 2
