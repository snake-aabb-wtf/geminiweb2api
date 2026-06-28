"""Tests for the ``/api/health/accounts`` self-check endpoint."""
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
async def test_health_accounts_probes_each_enabled_account(monkeypatch, server_module):
    from account_pool import Account
    server_module.pool.add(Account(
        name="good", f_sid="s", at="t", sn_param="n", bl_param="b", bound_models=["g"],
    ))
    server_module.pool.add(Account(
        name="bad", f_sid="s2", at="t2", sn_param="n2", bl_param="b2", bound_models=["g"],
    ))

    responses = {
        "good": ({"ok": True}, 0, 200),
        "bad": ({"error": "unauthorized"}, 0, 401),
    }

    async def fake_send(profile, account, messages, reqid, tools=None, attachments=None):
        payload, _new_reqid, status_code = responses[account.name]
        return payload, reqid + 1, status_code

    monkeypatch.setattr("adapter.send_request", fake_send)

    tr = httpx.ASGITransport(app=server_module.app)
    async with httpx.AsyncClient(transport=tr, base_url="http://t") as client:
        r = await client.post("/api/health/accounts")
    assert r.status_code == 200
    body = r.json()
    assert "results" in body
    by_name = {row["name"]: row for row in body["results"]}
    assert by_name["good"]["ok"] is True
    assert by_name["good"]["status_code"] == 200
    assert by_name["bad"]["ok"] is False
    assert by_name["bad"]["status_code"] == 401


@pytest.mark.asyncio
async def test_health_accounts_reports_timeout(monkeypatch, server_module):
    from account_pool import Account
    server_module.pool.add(Account(
        name="slow", f_sid="s", at="t", sn_param="n", bl_param="b", bound_models=["g"],
    ))

    async def hanging_send(*args, **kwargs):
        import asyncio
        # Sleep long enough that ``asyncio.wait_for(..., 0.05)`` cancels us.
        await asyncio.sleep(5)
        return {}, 0, 200

    # Force the timeout to a tiny value for the test.
    monkeypatch.setattr("adapter.send_request", hanging_send)
    monkeypatch.setattr(server_module, "HEALTH_CHECK_TIMEOUT", 0.05)

    tr = httpx.ASGITransport(app=server_module.app)
    async with httpx.AsyncClient(transport=tr, base_url="http://t", timeout=10) as client:
        r = await client.post("/api/health/accounts")
    body = r.json()
    by_name = {row["name"]: row for row in body["results"]}
    assert by_name["slow"]["ok"] is False
    assert by_name["slow"]["error"] == "timeout"
