"""Tests for the ``/v1/embeddings`` stub endpoint."""
from __future__ import annotations

import importlib
import os
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
    # Disable auth by deleting the env vars entirely (a sentinel value
    # like "" still gets parsed as a real key in some shells).
    for var in ("API_KEY", "ADMIN_KEY", "API_KEYS", "ADMIN_KEYS"):
        monkeypatch.delenv(var, raising=False)
    yield _reload_server(env)


@pytest.mark.asyncio
async def test_embeddings_disabled_by_default(server_module):
    tr = httpx.ASGITransport(app=server_module.app)
    async with httpx.AsyncClient(transport=tr, base_url="http://t") as client:
        r = await client.post("/v1/embeddings", json={"input": "hello"})
    assert r.status_code == 501
    assert r.json()["error"]["type"] == "embeddings_disabled"


@pytest.mark.asyncio
async def test_embeddings_returns_zero_vector_when_enabled(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("PROFILES=g\nDEFAULT_MODEL=g\nGEMINI_EMBEDDINGS_ENABLED=1\n", encoding="utf-8")
    monkeypatch.setenv("ENV_PATH", str(env))
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("ADMIN_KEY", "")
    server = _reload_server(env)
    try:
        tr = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=tr, base_url="http://t") as client:
            r = await client.post("/v1/embeddings", json={"input": ["a", "b"]})
        assert r.status_code == 200
        assert r.headers.get("x-gemini2api-status") == "stub"
        body = r.json()
        assert body["object"] == "list"
        assert len(body["data"]) == 2
        assert len(body["data"][0]["embedding"]) == 768
        assert all(v == 0.0 for v in body["data"][0]["embedding"])
    finally:
        importlib.reload(server)


@pytest.mark.asyncio
async def test_health_endpoint_reports_flags(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "PROFILES=g\nDEFAULT_MODEL=g\nGEMINI_EMBEDDINGS_ENABLED=1\nLOG_SCRUB_PII=1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENV_PATH", str(env))
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("ADMIN_KEY", "")
    server = _reload_server(env)
    try:
        tr = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=tr, base_url="http://t") as client:
            r = await client.get("/health")
        body = r.json()
        assert body["embeddings_enabled"] is True
        assert body["log_scrub_pii"] is True
    finally:
        importlib.reload(server)
