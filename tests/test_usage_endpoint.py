"""Tests for the ``/api/usage`` time-series endpoint."""
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
async def test_usage_endpoint_returns_series_and_summary(server_module):
    from account_pool import Account
    a = Account(name="a", bound_models=["g"], f_sid="s", at="t", sn_param="n", bl_param="b")
    server_module.pool.add(a)
    await server_module.pool.record_usage(a, "g", prompt_tokens=5, completion_tokens=10, total_tokens=15)

    tr = httpx.ASGITransport(app=server_module.app)
    async with httpx.AsyncClient(transport=tr, base_url="http://t") as client:
        r = await client.get("/api/usage?hours=1")
    assert r.status_code == 200
    body = r.json()
    assert body["hours"] == 1
    assert body["summary"]["total"] == 15
    assert body["summary"]["requests"] == 1
    # 1 hour = 60 minutes, so 61 points (inclusive of both ends).
    assert len(body["series"]) == 61
    non_zero = [p for p in body["series"] if p["total"] > 0]
    assert len(non_zero) == 1


@pytest.mark.asyncio
async def test_usage_endpoint_rejects_out_of_range(server_module):
    tr = httpx.ASGITransport(app=server_module.app)
    async with httpx.AsyncClient(transport=tr, base_url="http://t") as client:
        r = await client.get("/api/usage?hours=999")
    assert r.status_code == 422  # pydantic validation
